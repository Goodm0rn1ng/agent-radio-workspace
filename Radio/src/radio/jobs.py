"""In-process job manager used by the frontend API.

The CLI scripts remain the simplest way to run one task from a terminal. This
module gives a future frontend a small async boundary for long-running work:
submit a job, poll its status, and let the existing pipeline do the heavy lift.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel, Field

from radio.config import Settings
from radio.pipeline import run_pipeline
from radio.profiles import apply_prompt_profile, load_prompt_profile
from radio.radiko_playwright_source import record_radiko_via_playwright
from radio.radiko_source import (
    RadikoTimefreeSpec,
    parse_radiko_url,
    record_radiko_live,
    record_radiko_timefree,
)
from radio.recordings_layout import build_work_dir
from radio.state_store import StateStore
from radio.video_source import extract_audio_from_video_url
from radio.youtube_live_source import record_youtube_live


class JobStatus(StrEnum):
    QUEUED = "queued"
    WAITING = "waiting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class VideoWorkItem(BaseModel):
    url: str
    title: str | None = None
    playlist_index: int | None = None


class JobItemRecord(BaseModel):
    queue_index: int
    run_id: str | None = None
    url: str
    title: str | None = None
    playlist_index: int | None = None
    status: str = "queued"
    stage: str = "queued"
    message: str = "queued"
    work_dir: str | None = None
    error: str | None = None


class JobRecord(BaseModel):
    job_id: str
    kind: str
    run_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    stage: str = "queued"
    total: int = 1
    completed: int = 0
    current: str | None = None
    profile_id: str | None = None
    collection_id: str | None = None
    message: str = ""
    error: str | None = None
    results: list[dict[str, str | int | None]] = Field(default_factory=list)
    items: list[JobItemRecord] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class PreparedVideoItem:
    run_id: str
    url: str
    title: str
    playlist_index: int | None
    audio_path: Path
    work_dir: Path
    air_date: str


class JobManager:
    """Minimal in-memory background job registry for the local API server."""

    def __init__(self, settings: Settings, state_store: StateStore | None = None) -> None:
        self.settings = settings
        self._store = state_store or StateStore(settings.runtime.logs_dir.parent / "state.sqlite")
        self._store.initialize()
        stale_count = self._store.mark_stale_jobs_failed()
        if stale_count:
            logger.warning(f"已将 {stale_count} 个重启前未完成的 API job 标记为 failed")
        self._jobs: dict[str, JobRecord] = {
            payload["job_id"]: JobRecord(**payload) for payload in self._store.list_job_payloads()
        }
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pipeline_lock = asyncio.Lock()

    def list_jobs(self) -> list[JobRecord]:
        return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get_job(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def cancel_job(self, job_id: str) -> JobRecord | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED}:
            return job

        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()

        job.status = JobStatus.CANCELED
        job.error = None
        for item in job.items:
            if item.status not in {"succeeded", "failed", "canceled"}:
                item.status = "canceled"
                item.stage = "canceled"
                item.message = "canceled by user"
        job.finished_at = datetime.now()
        self._mark(job, stage="canceled", message="canceled by user")
        return job

    def list_artifacts(
        self,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        return self._store.list_artifacts(job_id=job_id, run_id=run_id, limit=limit)

    def start_video_batch(
        self,
        items: list[VideoWorkItem],
        *,
        air_date: str | None = None,
        fine_translation: bool = False,
        keep_audio: bool = False,
        cookies_path: Path | None = None,
        profile_id: str | None = None,
        collection_id: str | None = None,
    ) -> JobRecord:
        if not items:
            raise ValueError("至少需要一个视频 URL")
        job = self._new_job(kind="video_batch", total=len(items))
        job.profile_id = profile_id
        job.collection_id = collection_id or profile_id
        job.items = [
            JobItemRecord(
                queue_index=index,
                run_id=_make_run_id(job.job_id, index),
                url=item.url,
                title=item.title,
                playlist_index=item.playlist_index,
            )
            for index, item in enumerate(items, start=1)
        ]
        self._persist(job)
        self._tasks[job.job_id] = asyncio.create_task(
            self._run_video_batch(
                job.job_id,
                items,
                air_date=air_date,
                fine_translation=fine_translation,
                keep_audio=keep_audio,
                cookies_path=cookies_path,
                profile_id=profile_id,
                collection_id=collection_id,
            )
        )
        return job

    def start_live_recording(
        self,
        *,
        url: str,
        start_at: datetime,
        duration_minutes: int,
        title: str | None = None,
        air_date: str | None = None,
        fine_translation: bool = False,
        keep_audio: bool = False,
        cookies_path: Path | None = None,
        detection_timeout_minutes: int = 30,
        detection_interval_seconds: int = 60,
        profile_id: str | None = None,
        collection_id: str | None = None,
    ) -> JobRecord:
        if duration_minutes <= 0:
            raise ValueError("duration_minutes 必须大于 0")
        job = self._new_job(kind="live_recording", total=1)
        job.profile_id = profile_id
        job.collection_id = collection_id or profile_id
        job.run_id = _make_run_id(job.job_id, 1)
        job.items = [
            JobItemRecord(
                queue_index=1,
                run_id=job.run_id,
                url=url,
                title=title,
            )
        ]
        self._persist(job)
        self._tasks[job.job_id] = asyncio.create_task(
            self._run_live_recording(
                job.job_id,
                url=url,
                start_at=start_at,
                duration_minutes=duration_minutes,
                title=title,
                air_date=air_date,
                fine_translation=fine_translation,
                keep_audio=keep_audio,
                cookies_path=cookies_path,
                detection_timeout_minutes=detection_timeout_minutes,
                detection_interval_seconds=detection_interval_seconds,
                profile_id=profile_id,
                collection_id=collection_id,
            )
        )
        return job

    def start_radiko_recording(
        self,
        *,
        url: str,
        duration_minutes: int,
        start_at: datetime | None = None,
        title: str | None = None,
        air_date: str | None = None,
        fine_translation: bool = False,
        keep_audio: bool = False,
        cookies_path: Path | None = None,
        use_playwright: bool = True,
        cdp_url: str | None = None,
        profile_id: str | None = None,
        collection_id: str | None = None,
    ) -> JobRecord:
        if duration_minutes <= 0:
            raise ValueError("duration_minutes 必须大于 0")
        spec = parse_radiko_url(url, duration_minutes)
        if start_at is not None and not spec.is_live:
            raise ValueError("Radiko 回听链接会立即处理；预约开始时间只适用于 /live/ 实时链接")
        job = self._new_job(kind="radiko_recording", total=1)
        job.profile_id = profile_id
        job.collection_id = collection_id or profile_id
        job.run_id = _make_run_id(job.job_id, 1)
        job.items = [
            JobItemRecord(
                queue_index=1,
                run_id=job.run_id,
                url=url,
                title=title or spec.title_hint,
            )
        ]
        self._persist(job)
        self._tasks[job.job_id] = asyncio.create_task(
            self._run_radiko_recording(
                job.job_id,
                url=url,
                start_at=start_at,
                duration_minutes=duration_minutes,
                title=title,
                air_date=air_date,
                fine_translation=fine_translation,
                keep_audio=keep_audio,
                cookies_path=cookies_path,
                use_playwright=use_playwright,
                cdp_url=cdp_url,
                profile_id=profile_id,
                collection_id=collection_id,
            )
        )
        return job

    def _new_job(self, *, kind: str, total: int) -> JobRecord:
        job = JobRecord(job_id=uuid.uuid4().hex, kind=kind, total=total)
        self._mark(job, stage="queued", message="queued")
        self._jobs[job.job_id] = job
        return job

    async def _run_video_batch(
        self,
        job_id: str,
        items: list[VideoWorkItem],
        *,
        air_date: str | None,
        fine_translation: bool,
        keep_audio: bool,
        cookies_path: Path | None,
        profile_id: str | None = None,
        collection_id: str | None = None,
    ) -> None:
        job = self._jobs[job_id]
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now()
        self._mark(job, stage="download", message="prefetching queue")
        try:
            prepared_items: list[PreparedVideoItem] = []
            for index, item in enumerate(items, start=1):
                item_state = job.items[index - 1]
                job.current = item.url
                item_label = _video_item_label(index, len(items), item)
                self._mark_item(
                    job,
                    item_state,
                    status="running",
                    stage="download",
                    message="fetching audio",
                )
                self._mark(job, stage="download", message=f"prefetching {item_label}")
                try:
                    prepared = await self._prepare_video_item(
                        job_id,
                        item,
                        air_date=air_date,
                        cookies_path=cookies_path,
                        profile_id=profile_id,
                        collection_id=collection_id,
                        run_id=item_state.run_id or _make_run_id(job.job_id, index),
                    )
                except Exception as item_error:
                    result = {
                        "url": item.url,
                        "run_id": item_state.run_id,
                        "title": item.title,
                        "playlist_index": item.playlist_index,
                        "work_dir": None,
                        "error": f"{type(item_error).__name__}: {item_error}",
                    }
                    job.results.append(result)
                    self._mark_item(
                        job,
                        item_state,
                        status="failed",
                        stage="failed",
                        message="fetch failed",
                        error=result["error"],
                    )
                    job.status = JobStatus.FAILED
                    job.error = f"{item_label} fetch failed; batch stopped"
                    self._mark(job, stage="failed", message=job.error)
                    logger.exception(f"video batch item failed: {result['error']}")
                    return

                prepared_items.append(prepared)
                self._mark_item(
                    job,
                    item_state,
                    status="waiting",
                    stage="pipeline_waiting",
                    message="waiting for pipeline",
                    title=prepared.title,
                    work_dir=str(prepared.work_dir),
                )

            self._mark(
                job,
                stage="pipeline_waiting",
                message=f"{len(prepared_items)} items ready",
            )
            for index, (item, prepared) in enumerate(
                zip(items, prepared_items, strict=True),
                start=1,
            ):
                item_state = job.items[index - 1]
                job.current = item.url
                item_label = _video_item_label(index, len(items), item)
                try:
                    result = await self._run_prepared_video_item(
                        job,
                        item_state,
                        prepared,
                        fine_translation=fine_translation,
                        keep_audio=keep_audio,
                        profile_id=profile_id,
                    )
                except Exception as item_error:
                    result = {
                        "url": item.url,
                        "run_id": prepared.run_id,
                        "title": prepared.title,
                        "playlist_index": item.playlist_index,
                        "work_dir": str(prepared.work_dir),
                        "error": f"{type(item_error).__name__}: {item_error}",
                    }
                    job.results.append(result)
                    self._mark_item(
                        job,
                        item_state,
                        status="failed",
                        stage="failed",
                        message="pipeline failed",
                        error=result["error"],
                    )
                    job.status = JobStatus.FAILED
                    job.error = f"{item_label} pipeline failed; batch stopped"
                    self._mark(job, stage="failed", message=job.error)
                    logger.exception(f"video batch item failed: {result['error']}")
                    return

                job.results.append(result)
                job.completed = index
                self._mark_item(
                    job,
                    item_state,
                    status="succeeded",
                    stage="distributed",
                    message="delivered",
                )
                self._mark(job, stage="distributed", message=f"{item_label} delivered")
            job.status = JobStatus.SUCCEEDED
            self._mark(job, stage="distributed", message="done")
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = f"{type(e).__name__}: {e}"
            self._mark(job, stage="failed", message=job.error)
            logger.exception(f"video batch job failed: {job.error}")
        finally:
            job.finished_at = datetime.now()
            self._persist(job)

    async def _process_video_item(
        self,
        job_id: str,
        item: VideoWorkItem,
        *,
        air_date: str | None,
        fine_translation: bool,
        keep_audio: bool,
        cookies_path: Path | None,
        profile_id: str | None = None,
        collection_id: str | None = None,
    ) -> dict[str, str | int | None]:
        job = self._jobs[job_id]
        item_state = JobItemRecord(
            queue_index=1,
            run_id=_make_run_id(job.job_id, 1),
            url=item.url,
            title=item.title,
            playlist_index=item.playlist_index,
        )
        prepared = await self._prepare_video_item(
            job_id,
            item,
            air_date=air_date,
            cookies_path=cookies_path,
            profile_id=profile_id,
            collection_id=collection_id,
            run_id=item_state.run_id or _make_run_id(job.job_id, 1),
        )
        return await self._run_prepared_video_item(
            job,
            item_state,
            prepared,
            fine_translation=fine_translation,
            keep_audio=keep_audio,
            profile_id=profile_id,
        )

    async def _prepare_video_item(
        self,
        job_id: str,
        item: VideoWorkItem,
        *,
        air_date: str | None,
        cookies_path: Path | None,
        profile_id: str | None,
        collection_id: str | None,
        run_id: str,
    ) -> PreparedVideoItem:
        active_settings = self._settings_for_profile(profile_id)
        tmp_dir = (
            self.settings.runtime.recordings_dir / f".tmp_api_video_{job_id}_{int(time.time())}"
        )
        try:
            video_audio = await extract_audio_from_video_url(
                item.url,
                tmp_dir,
                cookies_path=cookies_path,
            )
            display_title = item.title or video_audio.title
            content_date = air_date or datetime.now().strftime("%Y-%m-%d")
            work_dir = build_work_dir(
                active_settings.runtime.recordings_dir,
                display_title,
                content_date,
                active_settings.summary.segments_library_path,
                source="video",
                collection_id=collection_id or profile_id,
            )
            work_dir.mkdir(parents=True, exist_ok=True)
            final_audio = work_dir / video_audio.audio_path.name
            shutil.move(str(video_audio.audio_path), str(final_audio))
            return PreparedVideoItem(
                run_id=run_id,
                url=item.url,
                title=display_title,
                playlist_index=item.playlist_index,
                audio_path=final_audio,
                work_dir=work_dir,
                air_date=content_date,
            )
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _run_prepared_video_item(
        self,
        job: JobRecord,
        item_state: JobItemRecord,
        prepared: PreparedVideoItem,
        *,
        fine_translation: bool,
        keep_audio: bool,
        profile_id: str | None,
    ) -> dict[str, str | int | None]:
        if self._pipeline_lock.locked():
            self._mark_item(
                job,
                item_state,
                status="waiting",
                stage="pipeline_waiting",
                message="waiting for pipeline",
            )
            self._mark(job, stage="pipeline_waiting", message="waiting for pipeline slot")
        async with self._pipeline_lock:
            self._mark_item(
                job,
                item_state,
                status="running",
                stage="pipeline",
                message="running pipeline",
            )
            await self._run_pipeline(
                job,
                prepared.audio_path,
                air_date=prepared.air_date,
                display_name=prepared.title,
                fine_translation=fine_translation,
                source="video",
                work_dir=prepared.work_dir,
                profile_id=profile_id,
                run_id=prepared.run_id,
            )
            if not keep_audio:
                prepared.audio_path.unlink(missing_ok=True)
            return {
                "url": prepared.url,
                "run_id": prepared.run_id,
                "title": prepared.title,
                "playlist_index": prepared.playlist_index,
                "collection_id": job.collection_id,
                "work_dir": str(prepared.work_dir),
            }

    async def _run_live_recording(
        self,
        job_id: str,
        *,
        url: str,
        start_at: datetime,
        duration_minutes: int,
        title: str | None,
        air_date: str | None,
        fine_translation: bool,
        keep_audio: bool,
        cookies_path: Path | None,
        detection_timeout_minutes: int,
        detection_interval_seconds: int,
        profile_id: str | None,
        collection_id: str | None,
    ) -> None:
        job = self._jobs[job_id]
        item_state = job.items[0] if job.items else None
        try:
            normalized_start = _normalize_start_at(start_at)
            delay_s = (normalized_start - datetime.now(normalized_start.tzinfo)).total_seconds()
            if delay_s > 0:
                job.status = JobStatus.WAITING
                self._mark(
                    job, stage="waiting", message=f"waiting until {normalized_start.isoformat()}"
                )
                if item_state is not None:
                    self._mark_item(
                        job,
                        item_state,
                        status="waiting",
                        stage="waiting",
                        message="waiting for start time",
                    )
                await asyncio.sleep(delay_s)

            job.status = JobStatus.RUNNING
            job.started_at = datetime.now()
            job.current = url
            self._mark(job, stage="recording", message="recording")
            if item_state is not None:
                self._mark_item(
                    job,
                    item_state,
                    status="running",
                    stage="recording",
                    message="recording",
                )

            tmp_dir = self.settings.runtime.recordings_dir / f".tmp_api_live_{job_id}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            try:
                if _is_radiko_live_url(url):
                    live_audio = await record_radiko_live(
                        url,
                        tmp_dir,
                        duration_minutes=duration_minutes,
                        title=title,
                    )
                    source = "radiko_live"
                else:
                    live_audio = await record_youtube_live(
                        url,
                        tmp_dir,
                        duration_minutes=duration_minutes,
                        title=title,
                        cookies_path=cookies_path,
                        detection_timeout_minutes=detection_timeout_minutes,
                        detection_interval_seconds=detection_interval_seconds,
                    )
                    source = "youtube_live"

                display_title = title or live_audio.title
                content_date = air_date or normalized_start.date().isoformat()
                active_settings = self._settings_for_profile(profile_id)
                work_dir = build_work_dir(
                    active_settings.runtime.recordings_dir,
                    display_title,
                    content_date,
                    active_settings.summary.segments_library_path,
                    source=source,
                    collection_id=collection_id or profile_id,
                )
                work_dir.mkdir(parents=True, exist_ok=True)
                final_audio = work_dir / live_audio.audio_path.name
                shutil.move(str(live_audio.audio_path), str(final_audio))
                if item_state is not None:
                    self._mark_item(
                        job,
                        item_state,
                        status="running",
                        stage="pipeline_waiting",
                        message="waiting for pipeline",
                        title=display_title,
                        work_dir=str(work_dir),
                    )

                await self._run_pipeline_exclusive(
                    job,
                    final_audio,
                    air_date=content_date,
                    display_name=display_title,
                    fine_translation=fine_translation,
                    source=source,
                    work_dir=work_dir,
                    profile_id=profile_id,
                    run_id=job.run_id,
                )
                if not keep_audio:
                    final_audio.unlink(missing_ok=True)
                job.completed = 1
                job.results.append(
                    {
                        "url": url,
                        "run_id": job.run_id,
                        "title": display_title,
                        "playlist_index": None,
                        "collection_id": job.collection_id,
                        "work_dir": str(work_dir),
                    }
                )
                job.status = JobStatus.SUCCEEDED
                if item_state is not None:
                    self._mark_item(
                        job,
                        item_state,
                        status="succeeded",
                        stage="distributed",
                        message="delivered",
                    )
                self._mark(job, stage="distributed", message="done")
            finally:
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = f"{type(e).__name__}: {e}"
            if item_state is not None:
                self._mark_item(
                    job,
                    item_state,
                    status="failed",
                    stage="failed",
                    message="job failed",
                    error=job.error,
                )
            self._mark(job, stage="failed", message=job.error)
            logger.exception(f"live recording job failed: {job.error}")
        finally:
            job.finished_at = datetime.now()
            self._persist(job)

    async def _run_radiko_recording(
        self,
        job_id: str,
        *,
        url: str,
        start_at: datetime | None,
        duration_minutes: int,
        title: str | None,
        air_date: str | None,
        fine_translation: bool,
        keep_audio: bool,
        cookies_path: Path | None,
        use_playwright: bool,
        cdp_url: str | None,
        profile_id: str | None,
        collection_id: str | None,
    ) -> None:
        job = self._jobs[job_id]
        item_state = job.items[0] if job.items else None
        try:
            spec = parse_radiko_url(url, duration_minutes)
            normalized_start = _normalize_start_at(start_at) if start_at is not None else None
            if spec.is_live and normalized_start is not None:
                delay_s = (normalized_start - datetime.now(normalized_start.tzinfo)).total_seconds()
                if delay_s > 0:
                    job.status = JobStatus.WAITING
                    self._mark(
                        job,
                        stage="waiting",
                        message=f"waiting until {normalized_start.isoformat()}",
                    )
                    if item_state is not None:
                        self._mark_item(
                            job,
                            item_state,
                            status="waiting",
                            stage="waiting",
                            message="waiting for start time",
                        )
                    await asyncio.sleep(delay_s)

            job.status = JobStatus.RUNNING
            job.started_at = datetime.now()
            job.current = url
            self._mark(job, stage="recording", message="recording radiko")
            if item_state is not None:
                self._mark_item(
                    job,
                    item_state,
                    status="running",
                    stage="recording",
                    message="recording",
                )

            tmp_dir = self.settings.runtime.recordings_dir / f".tmp_api_radiko_{job_id}"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            try:
                if spec.is_live:
                    radiko_audio = await record_radiko_live(
                        url,
                        tmp_dir,
                        duration_minutes=duration_minutes,
                        title=title,
                    )
                    source = "radiko_live"
                    content_date = (
                        air_date or (normalized_start or datetime.now()).date().isoformat()
                    )
                else:
                    if use_playwright:
                        radiko_audio = await record_radiko_via_playwright(
                            url,
                            tmp_dir,
                            duration_minutes=duration_minutes,
                            title=title,
                            cookies_path=cookies_path,
                            cdp_url=cdp_url,
                        )
                    else:
                        radiko_audio = await record_radiko_timefree(
                            url,
                            tmp_dir,
                            duration_minutes=duration_minutes,
                            title=title,
                        )
                    source = "radiko_timefree"
                    content_date = air_date or _radiko_timefree_air_date(spec)

                display_title = title or radiko_audio.title
                active_settings = self._settings_for_profile(profile_id)
                work_dir = build_work_dir(
                    active_settings.runtime.recordings_dir,
                    display_title,
                    content_date,
                    active_settings.summary.segments_library_path,
                    source=source,
                    collection_id=collection_id or profile_id,
                )
                work_dir.mkdir(parents=True, exist_ok=True)
                final_audio = work_dir / radiko_audio.audio_path.name
                shutil.move(str(radiko_audio.audio_path), str(final_audio))
                if item_state is not None:
                    self._mark_item(
                        job,
                        item_state,
                        status="running",
                        stage="pipeline_waiting",
                        message="waiting for pipeline",
                        title=display_title,
                        work_dir=str(work_dir),
                    )

                await self._run_pipeline_exclusive(
                    job,
                    final_audio,
                    air_date=content_date,
                    display_name=display_title,
                    fine_translation=fine_translation,
                    source=source,
                    work_dir=work_dir,
                    profile_id=profile_id,
                    run_id=job.run_id,
                )
                if not keep_audio:
                    final_audio.unlink(missing_ok=True)
                job.completed = 1
                job.results.append(
                    {
                        "url": url,
                        "run_id": job.run_id,
                        "title": display_title,
                        "playlist_index": None,
                        "collection_id": job.collection_id,
                        "work_dir": str(work_dir),
                    }
                )
                job.status = JobStatus.SUCCEEDED
                if item_state is not None:
                    self._mark_item(
                        job,
                        item_state,
                        status="succeeded",
                        stage="distributed",
                        message="delivered",
                    )
                self._mark(job, stage="distributed", message="done")
            finally:
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error = f"{type(e).__name__}: {e}"
            if item_state is not None:
                self._mark_item(
                    job,
                    item_state,
                    status="failed",
                    stage="failed",
                    message="job failed",
                    error=job.error,
                )
            self._mark(job, stage="failed", message=job.error)
            logger.exception(f"radiko recording job failed: {job.error}")
        finally:
            job.finished_at = datetime.now()
            self._persist(job)

    async def _run_pipeline_exclusive(
        self,
        job: JobRecord,
        audio_path: Path,
        *,
        air_date: str,
        display_name: str,
        fine_translation: bool,
        source: str,
        work_dir: Path,
        profile_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if self._pipeline_lock.locked():
            self._mark(job, stage="pipeline_waiting", message="waiting for pipeline slot")
        async with self._pipeline_lock:
            await self._run_pipeline(
                job,
                audio_path,
                air_date=air_date,
                display_name=display_name,
                fine_translation=fine_translation,
                source=source,
                work_dir=work_dir,
                profile_id=profile_id,
                run_id=run_id,
            )

    async def _run_pipeline(
        self,
        job: JobRecord,
        audio_path: Path,
        *,
        air_date: str,
        display_name: str,
        fine_translation: bool,
        source: str,
        work_dir: Path,
        profile_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self._mark(job, stage="pipeline", message="running pipeline")
        active_settings = self._settings_for_profile(profile_id)
        await run_pipeline(
            audio_path,
            active_settings,
            air_date=air_date,
            display_name=display_name,
            fine_translation=fine_translation,
            source=source,
            work_dir=work_dir,
            collection_id=job.collection_id,
            run_id=run_id,
        )

    def _mark(self, job: JobRecord, *, stage: str, message: str) -> None:
        _mark(job, stage=stage, message=message)
        self._persist(job)

    def _mark_item(
        self,
        job: JobRecord,
        item: JobItemRecord,
        *,
        status: str,
        stage: str,
        message: str,
        title: str | None = None,
        work_dir: str | None = None,
        error: str | None = None,
    ) -> None:
        _mark_item(
            item,
            status=status,
            stage=stage,
            message=message,
            title=title,
            work_dir=work_dir,
            error=error,
        )
        self._persist(job)

    def _persist(self, job: JobRecord) -> None:
        self._store.upsert_job_payload(job.model_dump(mode="json"))

    def _settings_for_profile(self, profile_id: str | None) -> Settings:
        if not profile_id:
            return self.settings
        profile = load_prompt_profile(profile_id)
        return apply_prompt_profile(self.settings, profile)


def _normalize_start_at(start_at: datetime) -> datetime:
    tz = ZoneInfo("Asia/Tokyo")
    if start_at.tzinfo is None:
        return start_at.replace(tzinfo=tz)
    return start_at.astimezone(tz)


def _is_radiko_live_url(url: str) -> bool:
    return "radiko.jp" in url and "/live/" in url


def _radiko_timefree_air_date(spec: RadikoTimefreeSpec) -> str:
    return datetime.strptime(spec.ft, "%Y%m%d%H%M%S").date().isoformat()


def _make_run_id(job_id: str, queue_index: int) -> str:
    return f"{job_id}-{queue_index:03d}"


def _mark(job: JobRecord, *, stage: str, message: str) -> None:
    job.stage = stage
    job.message = message
    timestamp = datetime.now().isoformat(timespec="seconds")
    job.logs.append(f"{timestamp}  {stage}: {message}")
    if len(job.logs) > 80:
        del job.logs[:-80]


def _mark_item(
    item: JobItemRecord,
    *,
    status: str,
    stage: str,
    message: str,
    title: str | None = None,
    work_dir: str | None = None,
    error: str | None = None,
) -> None:
    item.status = status
    item.stage = stage
    item.message = message
    if title is not None:
        item.title = title
    if work_dir is not None:
        item.work_dir = work_dir
    if error is not None:
        item.error = error


def _video_item_label(index: int, total: int, item: VideoWorkItem) -> str:
    label = f"item {index}/{total}"
    if item.playlist_index is not None:
        label += f" · playlist #{item.playlist_index}"
    if item.title:
        label += f" · {item.title}"
    return label
