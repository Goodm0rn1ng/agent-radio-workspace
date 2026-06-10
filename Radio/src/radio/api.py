"""FastAPI app for the future frontend."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from radio.approval import ApprovalStore, PendingSegment, default_approval_store_path
from radio.config import ScheduledProgramConfig, Settings, load_settings
from radio.jobs import JobManager, JobRecord, VideoWorkItem
from radio.playlist import PlaylistItem, expand_playlist_range
from radio.profiles import (
    PromptProfile,
    list_prompt_profiles,
    load_prompt_profile,
    save_prompt_profile,
)
from radio.recordings_layout import safe_collection_dir_name
from radio.segments_library import SegmentEntry, filter_library_by_series, load_segments_library
from radio.utils.logging import setup_logging


class PlaylistExpandRequest(BaseModel):
    playlist_url: str
    start_index: int = Field(gt=0)
    end_index: int = Field(gt=0)
    cookies_path: str | None = None


class VideoJobRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)
    playlist_url: str | None = None
    playlist_start_index: int | None = Field(default=None, gt=0)
    playlist_end_index: int | None = Field(default=None, gt=0)
    title: str | None = None
    title_template: str | None = None
    air_date: str | None = None
    cookies_path: str | None = None
    fine_translation: bool = False
    keep_audio: bool = False
    profile_id: str | None = None
    collection_id: str | None = None


class LiveJobRequest(BaseModel):
    url: str
    start_at: str
    duration_minutes: int = Field(gt=0)
    title: str | None = None
    air_date: str | None = None
    cookies_path: str | None = None
    detection_timeout_minutes: int = Field(default=30, ge=0)
    detection_interval_seconds: int = Field(default=60, ge=5)
    fine_translation: bool = False
    keep_audio: bool = False
    profile_id: str | None = None
    collection_id: str | None = None


class RadikoJobRequest(BaseModel):
    url: str
    start_at: str | None = None
    duration_minutes: int = Field(gt=0)
    title: str | None = None
    air_date: str | None = None
    cookies_path: str | None = None
    use_playwright: bool = True
    cdp_url: str | None = None
    fine_translation: bool = False
    keep_audio: bool = False
    profile_id: str | None = None
    collection_id: str | None = None


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    total: int


class CredentialStatus(BaseModel):
    configured: dict[str, bool]


class PromptProfileCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    translation_prompt: str
    summary_prompt: str
    terminology_path: str | None = None
    segments_library_path: str | None = None
    stt_prompt: str | None = None


class CollectionInfo(BaseModel):
    id: str
    name: str
    description: str = ""
    source: str = "custom"


class ScheduledProgramInfo(BaseModel):
    id: str
    name: str
    source_type: str
    source_label: str
    enabled: bool
    schedule_label: str
    duration_minutes: int
    url: str | None = None
    profile_id: str | None = None
    profile_name: str | None = None
    fine_translation: bool = False
    health_check: bool = False


class ScheduleCreate(BaseModel):
    timezone: str = "Asia/Tokyo"
    day_of_week: str = "mon"
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


class ScheduledProgramCreate(BaseModel):
    name: str
    source_type: str
    source_url: str
    schedule: ScheduleCreate
    enabled: bool = True
    duration_minutes: int = Field(default=30, gt=0)
    profile_id: str | None = None
    fine_translation: bool = False
    health_check: bool = True
    detection_timeout_minutes: int = Field(default=30, ge=0)
    detection_interval_seconds: int = Field(default=60, ge=5)


class ScheduledProgramUpdate(BaseModel):
    profile_id: str | None = None


class ArtifactInfo(BaseModel):
    id: int | None = None
    run_id: str | None = None
    job_id: str
    kind: str
    path: str
    label: str | None = None
    created_at: str
    payload: dict = Field(default_factory=dict)


class ArtifactFileInfo(BaseModel):
    name: str
    path: str
    kind: str
    size: int | None = None
    modified_at: str
    previewable: bool = False
    view_url: str | None = None
    download_url: str | None = None


class PendingDecisionResponse(BaseModel):
    segment: PendingSegment
    added: int = 0
    skipped: int = 0


app = FastAPI(title="Radio-Oshikatsu API")
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
SECRET_FIELDS = {
    "gemini_api_key": "GEMINI_API_KEY",
    "groq_api_key": "GROQ_API_KEY",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
}
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.on_event("startup")
async def _startup() -> None:
    config_path = os.environ.get("RADIO_CONFIG", "config/config.yaml")
    app.state.config_path = config_path
    settings = _load_api_settings(config_path)
    setup_logging(settings.runtime.logs_dir)
    app.state.manager = JobManager(settings)


def _manager(request: Request) -> JobManager:
    manager = getattr(request.app.state, "manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="API server is still starting")
    return manager


ManagerDep = Annotated[JobManager, Depends(_manager)]


@app.get("/api/health")
async def health(manager: ManagerDep) -> dict[str, str | int]:
    return {
        "status": "ok",
        "jobs": len(manager.list_jobs()),
    }


@app.get("/")
async def index() -> FileResponse:
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="frontend not built")
    return FileResponse(index_path)


@app.get("/api/credentials")
async def get_credentials() -> CredentialStatus:
    return CredentialStatus(configured=_credential_status())


@app.get("/api/profiles")
async def get_profiles() -> list[PromptProfile]:
    return list_prompt_profiles()


@app.post("/api/profiles")
async def create_profile(req: PromptProfileCreate) -> PromptProfile:
    try:
        return save_prompt_profile(
            profile_id=req.id,
            name=req.name,
            description=req.description,
            translation_prompt=req.translation_prompt,
            summary_prompt=req.summary_prompt,
            terminology_path=_optional_path(req.terminology_path),
            segments_library_path=_optional_path(req.segments_library_path),
            stt_prompt=req.stt_prompt,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/collections")
async def get_collections(manager: ManagerDep) -> list[CollectionInfo]:
    return _list_collections(manager.settings)


@app.get("/api/scheduler/programs")
async def get_scheduled_programs(manager: ManagerDep) -> list[ScheduledProgramInfo]:
    return [
        _scheduled_program_info(index, spec)
        for index, spec in enumerate(manager.settings.scheduled_programs, start=1)
    ]


@app.post("/api/scheduler/programs")
async def create_scheduled_program(
    req: ScheduledProgramCreate,
    request: Request,
    manager: ManagerDep,
) -> ScheduledProgramInfo:
    config_path = Path(getattr(request.app.state, "config_path", "config/config.yaml"))
    payload = _scheduled_program_payload(req)
    try:
        spec = ScheduledProgramConfig(**payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _append_scheduled_program(config_path, spec.model_dump(mode="json"))
    manager.settings = _load_api_settings(str(config_path))
    return _scheduled_program_info(len(manager.settings.scheduled_programs), spec)


@app.patch("/api/scheduler/programs/{program_id}")
async def update_scheduled_program(
    program_id: str,
    req: ScheduledProgramUpdate,
    request: Request,
    manager: ManagerDep,
) -> ScheduledProgramInfo:
    index = _scheduled_program_index(program_id)
    config_path = Path(getattr(request.app.state, "config_path", "config/config.yaml"))
    profile_id = _normalize_profile_id(req.profile_id)
    _validate_profile_id(profile_id)
    _update_scheduled_program_profile(config_path, index, profile_id)
    manager.settings = _load_api_settings(str(config_path))
    try:
        spec = manager.settings.scheduled_programs[index - 1]
    except IndexError as e:
        raise HTTPException(status_code=404, detail="定时计划不存在") from e
    return _scheduled_program_info(index, spec)


@app.get("/api/knowledge/library")
async def get_library(
    manager: ManagerDep,
    program_series: str | None = None,
) -> list[SegmentEntry]:
    library = load_segments_library(manager.settings.summary.segments_library_path)
    if program_series:
        library = filter_library_by_series(library, program_series)
    return library


@app.get("/api/knowledge/pending")
async def get_pending_segments(manager: ManagerDep, limit: int = 50) -> list[PendingSegment]:
    store = ApprovalStore(default_approval_store_path(manager.settings.runtime.logs_dir))
    return store.list_pending(limit)


@app.post("/api/knowledge/pending/{segment_id}/approve")
async def approve_pending_segment(
    segment_id: str,
    manager: ManagerDep,
) -> PendingDecisionResponse:
    store = ApprovalStore(default_approval_store_path(manager.settings.runtime.logs_dir))
    try:
        segment, added, skipped = store.approve(
            segment_id,
            manager.settings.summary.segments_library_path,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail="pending segment not found") from e
    return PendingDecisionResponse(segment=segment, added=added, skipped=skipped)


@app.post("/api/knowledge/pending/{segment_id}/skip")
async def skip_pending_segment(
    segment_id: str,
    manager: ManagerDep,
) -> PendingDecisionResponse:
    store = ApprovalStore(default_approval_store_path(manager.settings.runtime.logs_dir))
    try:
        segment = store.skip(segment_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail="pending segment not found") from e
    return PendingDecisionResponse(segment=segment)


@app.get("/api/jobs")
async def list_jobs(manager: ManagerDep) -> list[JobRecord]:
    return manager.list_jobs()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, manager: ManagerDep) -> JobRecord:
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, manager: ManagerDep) -> JobRecord:
    job = manager.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/artifacts")
async def list_artifacts(
    manager: ManagerDep,
    job_id: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> list[ArtifactInfo]:
    return [
        ArtifactInfo(**item)
        for item in manager.list_artifacts(job_id=job_id, run_id=run_id, limit=limit)
    ]


@app.get("/api/artifacts/files")
async def list_artifact_files(path: str, manager: ManagerDep) -> list[ArtifactFileInfo]:
    target = _resolve_recording_path(manager.settings, path)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="path 不是目录")
    items = [_artifact_file_info(child) for child in sorted(target.iterdir(), key=_file_sort_key)]
    return items


@app.get("/api/artifacts/file")
async def get_artifact_file(
    path: str,
    manager: ManagerDep,
    download: bool = False,
) -> FileResponse:
    target = _resolve_recording_path(manager.settings, path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="path 不是文件")
    filename = target.name if download else None
    return FileResponse(target, filename=filename)


@app.websocket("/api/jobs/ws")
async def jobs_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            manager = getattr(websocket.app.state, "manager", None)
            jobs = []
            metrics = []
            if manager is not None:
                jobs = [job.model_dump(mode="json") for job in manager.list_jobs()]
                metrics = _read_recent_metrics(manager.settings.runtime.logs_dir / "metrics.jsonl")
            await websocket.send_text(
                json.dumps(
                    {
                        "jobs": jobs,
                        "metrics": metrics,
                        "credentials": _credential_status(),
                    },
                    ensure_ascii=False,
                )
            )
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


@app.get("/api/metrics/recent")
async def recent_metrics(manager: ManagerDep, limit: int = 20) -> list[dict]:
    return _read_recent_metrics(manager.settings.runtime.logs_dir / "metrics.jsonl", limit=limit)


@app.post("/api/playlists/expand")
async def expand_playlist(req: PlaylistExpandRequest) -> list[PlaylistItem]:
    return await expand_playlist_range(
        req.playlist_url,
        start_index=req.start_index,
        end_index=req.end_index,
        cookies_path=_optional_path(req.cookies_path),
    )


@app.post("/api/video-jobs")
async def create_video_job(req: VideoJobRequest, manager: ManagerDep) -> JobCreateResponse:
    items: list[VideoWorkItem] = [VideoWorkItem(url=url) for url in req.urls]
    cookies_path = _optional_path(req.cookies_path)

    if req.playlist_url is not None:
        if req.playlist_start_index is None or req.playlist_end_index is None:
            raise HTTPException(
                status_code=400,
                detail="playlist_start_index 和 playlist_end_index 必须同时提供",
            )
        playlist_items = await expand_playlist_range(
            req.playlist_url,
            start_index=req.playlist_start_index,
            end_index=req.playlist_end_index,
            cookies_path=cookies_path,
        )
        for playlist_item in playlist_items:
            items.append(
                VideoWorkItem(
                    url=playlist_item.url,
                    title=_render_title(req.title_template, playlist_item),
                    playlist_index=playlist_item.index,
                )
            )

    if req.title and len(items) == 1:
        items[0].title = req.title
    if not items:
        raise HTTPException(status_code=400, detail="至少提供 urls 或 playlist_url")

    job = manager.start_video_batch(
        items,
        air_date=req.air_date,
        fine_translation=req.fine_translation,
        keep_audio=req.keep_audio,
        cookies_path=cookies_path,
        profile_id=req.profile_id,
        collection_id=_optional_collection_id(req.collection_id),
    )
    return JobCreateResponse(job_id=job.job_id, status=job.status, total=job.total)


@app.post("/api/live-jobs")
async def create_live_job(req: LiveJobRequest, manager: ManagerDep) -> JobCreateResponse:
    try:
        start_at = datetime.fromisoformat(req.start_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="start_at 必须是 ISO datetime") from e

    job = manager.start_live_recording(
        url=req.url,
        start_at=start_at,
        duration_minutes=req.duration_minutes,
        title=req.title,
        air_date=req.air_date,
        fine_translation=req.fine_translation,
        keep_audio=req.keep_audio,
        cookies_path=_optional_path(req.cookies_path),
        detection_timeout_minutes=req.detection_timeout_minutes,
        detection_interval_seconds=req.detection_interval_seconds,
        profile_id=req.profile_id,
        collection_id=_optional_collection_id(req.collection_id),
    )
    return JobCreateResponse(job_id=job.job_id, status=job.status, total=job.total)


@app.post("/api/radiko-jobs")
async def create_radiko_job(req: RadikoJobRequest, manager: ManagerDep) -> JobCreateResponse:
    start_at = None
    if req.start_at:
        try:
            start_at = datetime.fromisoformat(req.start_at)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="start_at 必须是 ISO datetime") from e

    try:
        job = manager.start_radiko_recording(
            url=req.url,
            start_at=start_at,
            duration_minutes=req.duration_minutes,
            title=req.title,
            air_date=req.air_date,
            fine_translation=req.fine_translation,
            keep_audio=req.keep_audio,
            cookies_path=_optional_path(req.cookies_path),
            use_playwright=req.use_playwright,
            cdp_url=req.cdp_url,
            profile_id=req.profile_id,
            collection_id=_optional_collection_id(req.collection_id),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JobCreateResponse(job_id=job.job_id, status=job.status, total=job.total)


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _optional_collection_id(value: str | None) -> str | None:
    if not value:
        return None
    collection_id = safe_collection_dir_name(value)
    if not collection_id:
        raise HTTPException(status_code=400, detail="collection_id 无效")
    return collection_id


def _resolve_recording_path(settings: Settings, value: str) -> Path:
    if not value:
        raise HTTPException(status_code=400, detail="path 必填")
    base = settings.runtime.recordings_dir.resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        direct = candidate.resolve()
        try:
            direct.relative_to(base)
            candidate = direct
        except (OSError, ValueError):
            candidate = base / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(base)
    except (OSError, ValueError) as e:
        raise HTTPException(status_code=403, detail="path 不在 recordings 目录内") from e
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="path 不存在")
    return resolved


def _artifact_file_info(path: Path) -> ArtifactFileInfo:
    stat = path.stat()
    path_text = str(path)
    if path.is_file():
        encoded = quote(path_text, safe="")
        view_url = f"/api/artifacts/file?path={encoded}"
        download_url = f"{view_url}&download=1"
    else:
        view_url = None
        download_url = None
    return ArtifactFileInfo(
        name=path.name,
        path=path_text,
        kind="dir" if path.is_dir() else "file",
        size=stat.st_size if path.is_file() else None,
        modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        previewable=path.is_file() and _is_previewable_artifact(path),
        view_url=view_url,
        download_url=download_url,
    )


def _file_sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name.lower())


def _is_previewable_artifact(path: Path) -> bool:
    return path.suffix.lower() in {".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".log"}


def _list_collections(settings: Settings) -> list[CollectionInfo]:
    collections: dict[str, CollectionInfo] = {}
    for profile in list_prompt_profiles():
        collections[profile.id] = CollectionInfo(
            id=profile.id,
            name=profile.name,
            description=profile.description,
            source="profile",
        )

    recordings_dir = settings.runtime.recordings_dir
    if recordings_dir.exists():
        for path in sorted(recordings_dir.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            collections.setdefault(
                path.name,
                CollectionInfo(id=path.name, name=path.name, source="folder"),
            )
    return sorted(collections.values(), key=lambda item: (item.source != "profile", item.name))


def _scheduled_program_info(index: int, spec) -> ScheduledProgramInfo:
    profile_name = None
    if spec.profile_id:
        profile = next(
            (item for item in list_prompt_profiles() if item.id == spec.profile_id),
            None,
        )
        profile_name = profile.name if profile else spec.profile_id
    return ScheduledProgramInfo(
        id=f"program-{index}",
        name=spec.name,
        source_type=spec.source_type,
        source_label=_source_type_label(spec.source_type),
        enabled=spec.enabled,
        schedule_label=_schedule_label(spec.schedule),
        duration_minutes=spec.duration_minutes,
        url=_scheduled_program_url(spec),
        profile_id=spec.profile_id,
        profile_name=profile_name,
        fine_translation=spec.fine_translation,
        health_check=spec.health_check,
    )


def _scheduled_program_payload(req: ScheduledProgramCreate) -> dict:
    source_type = req.source_type.strip()
    source_url = req.source_url.strip()
    if source_type not in {"radiko_live", "radiko_timefree", "youtube_live"}:
        raise HTTPException(status_code=400, detail="来源类型无效")
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="节目名称必填")
    if not source_url:
        raise HTTPException(status_code=400, detail="来源链接必填")

    payload: dict = {
        "name": req.name.strip(),
        "source_type": source_type,
        "enabled": req.enabled,
        "schedule": req.schedule.model_dump(),
        "duration_minutes": req.duration_minutes,
        "profile_id": _normalize_profile_id(req.profile_id),
        "fine_translation": req.fine_translation,
        "health_check": req.health_check,
        "detection_timeout_minutes": req.detection_timeout_minutes,
        "detection_interval_seconds": req.detection_interval_seconds,
    }
    _validate_profile_id(payload["profile_id"])
    if source_type == "radiko_live":
        payload["radiko_station_id"] = _radiko_station_from_live_url(source_url)
    elif source_type == "radiko_timefree":
        if "/ts/" not in source_url:
            raise HTTPException(status_code=400, detail="Radiko 回听计划需要 time-free 链接")
        payload["radiko_timefree_url"] = source_url
        payload["health_check"] = False
    else:
        payload["channel_live_url"] = source_url
        payload["health_check"] = False
    return payload


def _normalize_profile_id(profile_id: str | None) -> str | None:
    profile_id = (profile_id or "").strip()
    return profile_id or None


def _validate_profile_id(profile_id: str | None) -> None:
    if not profile_id:
        return
    try:
        load_prompt_profile(profile_id)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _scheduled_program_index(program_id: str) -> int:
    match = re.fullmatch(r"program-(\d+)", program_id)
    if not match:
        raise HTTPException(status_code=400, detail="定时计划 id 无效")
    return int(match.group(1))


def _radiko_station_from_live_url(value: str) -> str:
    if re.fullmatch(r"[A-Z0-9]+", value):
        return value
    match = re.search(r"/live/([A-Z0-9]+)", value)
    if not match:
        raise HTTPException(status_code=400, detail="Radiko 实时计划需要 live 链接或电台 ID")
    return match.group(1)


def _append_scheduled_program(config_path: Path, payload: dict) -> None:
    item_text = _yaml_list_item(payload)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    if re.search(r"(?m)^scheduled_programs:\s*\[\]\s*$", text):
        updated = re.sub(
            r"(?m)^scheduled_programs:\s*\[\]\s*$",
            f"scheduled_programs:\n{item_text.rstrip()}",
            text,
            count=1,
        )
    elif re.search(r"(?m)^scheduled_programs:\s*$", text):
        match = re.search(r"(?m)^scheduled_programs:\s*$", text)
        assert match is not None
        next_key = re.search(r"\n(?=[A-Za-z_][A-Za-z0-9_-]*:)", text[match.end() :])
        if next_key:
            insert_at = match.end() + next_key.start()
            updated = f"{text[:insert_at]}\n{item_text.rstrip()}{text[insert_at:]}"
        else:
            updated = f"{text.rstrip()}\n{item_text}"
    else:
        updated = (
            f"{text.rstrip()}\n\nscheduled_programs:\n{item_text}"
            if text
            else f"scheduled_programs:\n{item_text}"
        )
    config_path.write_text(updated, encoding="utf-8")


def _update_scheduled_program_profile(
    config_path: Path,
    index: int,
    profile_id: str | None,
) -> None:
    text = config_path.read_text(encoding="utf-8")
    items = _scheduled_program_item_spans(text)
    if index < 1 or index > len(items):
        raise HTTPException(status_code=404, detail="定时计划不存在")

    start, end = items[index - 1]
    block = text[start:end]
    profile_line = f'    profile_id: "{profile_id}"\n' if profile_id else ""
    if re.search(r"(?m)^    profile_id:\s*.*$", block):
        if profile_id:
            block = re.sub(
                r"(?m)^    profile_id:\s*.*$",
                profile_line.rstrip("\n"),
                block,
                count=1,
            )
        else:
            block = re.sub(r"(?m)^    profile_id:\s*.*\n?", "", block, count=1)
    elif profile_id:
        match = re.search(r"(?m)^    fine_translation:\s*.*$", block)
        if match:
            insert_at = match.end()
            block = f"{block[:insert_at]}\n{profile_line.rstrip()}{block[insert_at:]}"
        else:
            block = block.rstrip("\n") + "\n" + profile_line

    config_path.write_text(text[:start] + block + text[end:], encoding="utf-8")


def _scheduled_program_item_spans(text: str) -> list[tuple[int, int]]:
    match = re.search(r"(?m)^scheduled_programs:\s*$", text)
    if not match:
        return []
    section_start = match.end()
    next_key = re.search(r"\n(?=[A-Za-z_][A-Za-z0-9_-]*:)", text[section_start:])
    section_end = (
        section_start + next_key.start()
        if next_key is not None
        else len(text)
    )
    section = text[section_start:section_end]
    starts = [section_start + item.start() for item in re.finditer(r"(?m)^  - ", section)]
    return [
        (start, starts[pos + 1] if pos + 1 < len(starts) else section_end)
        for pos, start in enumerate(starts)
    ]


def _yaml_list_item(payload: dict) -> str:
    lines = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).splitlines()
    if not lines:
        return ""
    return "  - " + lines[0] + "\n" + "\n".join(f"    {line}" for line in lines[1:]) + "\n"


def _source_type_label(source_type: str) -> str:
    return {
        "radiko_live": "Radiko 实时录制",
        "radiko_timefree": "Radiko 回听录制",
        "youtube_live": "YouTube 直播录制",
    }.get(source_type, source_type)


def _schedule_label(schedule) -> str:
    day = {
        "mon": "周一",
        "tue": "周二",
        "wed": "周三",
        "thu": "周四",
        "fri": "周五",
        "sat": "周六",
        "sun": "周日",
    }.get(schedule.day_of_week, schedule.day_of_week)
    return f"每{day} {schedule.hour:02d}:{schedule.minute:02d}（{schedule.timezone}）"


def _scheduled_program_url(spec) -> str | None:
    if spec.source_type == "radiko_live" and spec.radiko_station_id:
        return f"https://radiko.jp/#!/live/{spec.radiko_station_id}"
    if spec.source_type == "radiko_timefree" and spec.radiko_timefree_url:
        return spec.radiko_timefree_url
    if spec.source_type == "youtube_live" and spec.channel_live_url:
        return spec.channel_live_url
    return None


def _render_title(template: str | None, item: PlaylistItem) -> str | None:
    if not template:
        return None
    return template.format(index=item.index, title=item.title)


def _load_api_settings(config_path: str) -> Settings:
    env_path = Path(config_path).resolve().parent.parent / ".env"
    app.state.env_path = env_path
    _ensure_secret_defaults(env_path)
    return load_settings(config_path)


def _ensure_secret_defaults(env_path: Path) -> None:
    file_values = _read_env_values(env_path)
    for env_name in SECRET_FIELDS.values():
        if env_name not in os.environ and env_name not in file_values:
            os.environ[env_name] = ""


def _credential_status() -> dict[str, bool]:
    env_path = getattr(app.state, "env_path", Path(".env"))
    file_values = _read_env_values(Path(env_path))
    status: dict[str, bool] = {}
    for field_name, env_name in SECRET_FIELDS.items():
        value = os.environ.get(env_name)
        if value is None:
            value = file_values.get(env_name, "")
        status[field_name] = bool(value)
    return status


def _read_env_values(env_path: Path) -> dict[str, str]:
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _read_recent_metrics(path: Path, limit: int = 20) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))
