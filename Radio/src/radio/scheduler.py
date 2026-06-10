"""APScheduler 守护进程：按 config 中每个 ScheduledProgramConfig 的 cron 字段
到点触发录制 → pipeline → Telegram。

特性：
- AsyncIOScheduler（跟 pipeline 的 asyncio 兼容）
- SQLAlchemyJobStore + SQLite（重启自动恢复 jobs）
- 每个节目一个 job，job_id = program 的 source_type + station_id + slug
- 失败不重试整个 job（API 失败已在内部各模块用 @async_retry 处理）；
  改用 metrics + Telegram failure notification 记录失败

调度器入口：scripts/main_daemon.py 调 `start_scheduler(settings)` 然后跑 forever。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from radio.config import ScheduledProgramConfig, Settings
from radio.recordings_layout import build_work_dir


def _job_id_for(spec: ScheduledProgramConfig) -> str:
    """生成稳定 job_id，便于重启后命中 SQLite 中的同一行。"""
    parts = [spec.source_type]
    if spec.radiko_station_id:
        parts.append(spec.radiko_station_id)
    if spec.channel_live_url:
        # 取 channel_live_url 末尾的 handle
        parts.append(spec.channel_live_url.rstrip("/").split("/")[-1][:16])
    # 加 program name 的简短 hash 防同 station 多节目重复
    import hashlib
    parts.append(hashlib.sha1(spec.name.encode("utf-8")).hexdigest()[:6])
    return "_".join(parts)


def _now_for_schedule(spec: ScheduledProgramConfig) -> datetime:
    """Return a timezone-local naive datetime for schedule calculations."""
    return datetime.now(ZoneInfo(spec.schedule.timezone)).replace(tzinfo=None)


def _radiko_timefree_start_for_run(
    spec: ScheduledProgramConfig,
    as_of: datetime | None = None,
) -> datetime:
    """Resolve which weekly Radiko time-free occurrence this job should process.

    `radiko_timefree_url` is treated as the seed occurrence. For example:
    seed 2026-05-18 00:30 + interval_days=7 resolves to 2026-05-25 00:30
    when the scheduler fires on 2026-05-25 after that start time.
    """
    from radio.radiko_source import RadikoTimefreeSpec, parse_radiko_url

    if not spec.radiko_timefree_url:
        raise ValueError("radiko_timefree 需要配置 radiko_timefree_url")
    if spec.interval_days <= 0:
        raise ValueError("interval_days 必须大于 0")

    parsed = parse_radiko_url(spec.radiko_timefree_url, spec.duration_minutes)
    if not isinstance(parsed, RadikoTimefreeSpec):
        raise ValueError("radiko_timefree_url 必须是 /ts/STATION/YYYYMMDDhhmmss URL")

    seed = datetime.strptime(parsed.ft, "%Y%m%d%H%M%S")
    if as_of is None:
        as_of = _now_for_schedule(spec)
    elif as_of.tzinfo is not None:
        as_of = as_of.astimezone(ZoneInfo(spec.schedule.timezone)).replace(tzinfo=None)

    if as_of < seed:
        return seed

    interval = timedelta(days=spec.interval_days)
    elapsed_intervals = int((as_of - seed).total_seconds() // interval.total_seconds())
    return seed + interval * elapsed_intervals


def _radiko_timefree_url_for_run(
    spec: ScheduledProgramConfig,
    as_of: datetime | None = None,
) -> str:
    """Build the concrete Radiko time-free URL for the current recurrence."""
    from radio.radiko_source import RadikoTimefreeSpec, parse_radiko_url

    parsed = parse_radiko_url(spec.radiko_timefree_url, spec.duration_minutes)
    if not isinstance(parsed, RadikoTimefreeSpec):
        raise ValueError("radiko_timefree_url 必须是 /ts/STATION/YYYYMMDDhhmmss URL")
    start = _radiko_timefree_start_for_run(spec, as_of=as_of)
    return f"https://radiko.jp/#!/ts/{parsed.station_id}/{start.strftime('%Y%m%d%H%M%S')}"


async def _run_scheduled_program(
    spec_dict: dict[str, Any],
    settings_yaml_path: str,
) -> None:
    """APScheduler 调用的入口 — 必须可序列化所以参数是 dict + path（不传 Settings）。

    内部重新 load settings + 把 dict 转回 ScheduledProgramConfig，跑录制 + pipeline。
    """
    from radio.config import load_settings
    from radio.health import health_check_before_record, notify_health_failure
    from radio.pipeline import run_pipeline
    from radio.profiles import apply_prompt_profile, load_prompt_profile
    from radio.radiko_source import record_radiko_live, record_radiko_timefree
    from radio.utils.logging import setup_logging
    from radio.youtube_live_source import record_youtube_live

    settings = load_settings(settings_yaml_path)
    setup_logging(settings.runtime.logs_dir)

    spec = ScheduledProgramConfig(**spec_dict)
    active_settings = settings
    if spec.profile_id:
        active_settings = apply_prompt_profile(settings, load_prompt_profile(spec.profile_id))
        logger.info(f"应用提示词方案：{spec.profile_id}")

    logger.info(
        f"⏰ 调度触发：{spec.name} (source={spec.source_type}, station={spec.radiko_station_id})"
    )

    # 录制前健康检查
    if spec.health_check and spec.source_type == "radiko_live" and spec.radiko_station_id:
        health = await health_check_before_record(
            spec.radiko_station_id,
            pre_record_minutes=spec.health_pre_record_minutes,
            do_wait=spec.health_pre_record_minutes > 0,
        )
        if health.ok:
            logger.success(
                f"✓ 健康检查通过：{health.station_id} 当前在播「{health.current_program_title}」"
            )
        else:
            logger.error(f"✗ 健康检查失败：{health.detail}")
            try:
                await notify_health_failure(settings, health, program_name=spec.name)
            except Exception as e:
                logger.error(f"health 告警发送失败：{e!r}")
            if spec.fail_on_health_fail:
                logger.error("--fail-on-health-fail 启用，本次跳过")
                return
            logger.warning("健康检查失败但继续尝试录制（配置 fail_on_health_fail 可改）")

    # 算 work_dir + 录制
    air_date = _now_for_schedule(spec).date().isoformat()
    radiko_timefree_url: str | None = None
    if spec.source_type == "radiko_timefree":
        start = _radiko_timefree_start_for_run(spec)
        air_date = start.date().isoformat()
        radiko_timefree_url = _radiko_timefree_url_for_run(spec, as_of=start)

    work_dir = build_work_dir(
        active_settings.runtime.recordings_dir,
        spec.name,
        air_date,
        active_settings.summary.segments_library_path,
        source=spec.source_type,
        collection_id=spec.profile_id,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"work_dir: {work_dir}")

    try:
        if spec.source_type == "radiko_live":
            url = f"https://radiko.jp/#!/live/{spec.radiko_station_id}"
            radiko_audio = await record_radiko_live(
                url,
                work_dir,
                duration_minutes=spec.duration_minutes,
                title=spec.name,
            )
            audio_path = radiko_audio.audio_path
        elif spec.source_type == "radiko_timefree":
            if radiko_timefree_url is None:
                radiko_timefree_url = _radiko_timefree_url_for_run(spec)
            logger.info(f"Radiko time-free URL: {radiko_timefree_url}")
            radiko_audio = await record_radiko_timefree(
                radiko_timefree_url,
                work_dir,
                duration_minutes=spec.duration_minutes,
                title=spec.name,
            )
            audio_path = radiko_audio.audio_path
        elif spec.source_type == "youtube_live":
            if not spec.channel_live_url:
                raise ValueError("youtube_live 需要配置 channel_live_url")
            youtube_audio = await record_youtube_live(
                spec.channel_live_url,
                work_dir,
                duration_minutes=spec.duration_minutes,
                title=spec.name,
                cookies_path=spec.cookies_path,
                detection_timeout_minutes=spec.detection_timeout_minutes,
                detection_interval_seconds=spec.detection_interval_seconds,
            )
            audio_path = youtube_audio.audio_path
        else:
            raise NotImplementedError(
                f"source_type={spec.source_type} 调度模式暂未实现"
            )

        await run_pipeline(
            audio_path,
            active_settings,
            air_date=air_date,
            display_name=spec.name,
            fine_translation=spec.fine_translation,
            source=spec.source_type,
            work_dir=work_dir,
        )
        # 录制完默认删 audio 节省空间（调度器模式无 --keep-audio）
        audio_path.unlink(missing_ok=True)
        logger.success(f"📦 调度任务完成：{spec.name}")
    except Exception as e:
        logger.exception(f"调度任务失败：{e!r}")
        # pipeline 内部已有 notify_pipeline_failure，外层 raise 让 APScheduler 记录
        raise


def build_scheduler(settings: Settings, settings_yaml_path: str) -> AsyncIOScheduler:
    """构造 AsyncIOScheduler，注册所有 enabled 节目。"""
    jobstore_path = settings.scheduler.jobstore_path
    jobstore_path.parent.mkdir(parents=True, exist_ok=True)
    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{jobstore_path}"),
    }
    scheduler = AsyncIOScheduler(jobstores=jobstores)

    if not settings.scheduled_programs:
        logger.warning(
            "config.yaml 中 scheduled_programs 为空——调度器启动后没有任何节目可跑。"
            "请在 config.yaml 加 scheduled_programs 列表。"
        )
        return scheduler

    for spec in settings.scheduled_programs:
        if not spec.enabled:
            logger.info(f"跳过 disabled 节目：{spec.name}")
            continue
        job_id = _job_id_for(spec)
        trigger = CronTrigger(
            day_of_week=spec.schedule.day_of_week,
            hour=spec.schedule.hour,
            minute=spec.schedule.minute,
            timezone=spec.schedule.timezone,
        )
        scheduler.add_job(
            _run_scheduled_program,
            trigger=trigger,
            args=[spec.model_dump(), settings_yaml_path],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=settings.scheduler.misfire_grace_seconds,
            coalesce=True,  # 错过多次只补跑一次
        )
        logger.info(
            f"已注册节目：{spec.name}  cron={spec.schedule.day_of_week} "
            f"{spec.schedule.hour:02d}:{spec.schedule.minute:02d} "
            f"{spec.schedule.timezone}  job_id={job_id}"
        )

    return scheduler


async def start_scheduler_and_wait(settings: Settings, settings_yaml_path: str) -> None:
    """同步入口：启动调度器后挂在事件循环里直到 Ctrl+C / 异常。"""
    scheduler = build_scheduler(settings, settings_yaml_path)
    scheduler.start()
    n_jobs = len(scheduler.get_jobs())
    logger.success(f"✅ 调度器启动完成，{n_jobs} 个任务挂载")
    for job in scheduler.get_jobs():
        next_run = job.next_run_time
        logger.info(f"  · {job.id}  下次触发：{next_run}")

    # 永久阻塞
    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
