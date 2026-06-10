"""主 pipeline：音频文件 → STT → 翻译 → 名词修正 → transcript.txt → 总结 → Telegram。

v0.3.0 起每次跑会写一行 jsonl 到 `data/logs/metrics.jsonl`；pipeline 失败时
自动发简短错误到 Telegram。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx
from loguru import logger

from radio.approval import ApprovalStore, default_approval_store_path
from radio.config import Settings
from radio.history import append_history_entry
from radio.recordings_layout import build_work_dir
from radio.segmenter import segment_audio
from radio.segments_library import extract_series_name
from radio.stt import transcribe_segments
from radio.summarize import dump_summary_to_disk, summarize
from radio.telegram_sender import notify_pipeline_failure, send_to_telegram
from radio.terminology import apply_terminology_corrections, load_post_corrections
from radio.transcript import apply_name_corrections, write_bilingual_txt
from radio.translate import translate_segments
from radio.utils.metrics import MetricsCollector


async def run_pipeline(
    audio_path: Path,
    settings: Settings,
    air_date: str | None = None,
    display_name: str | None = None,
    fine_translation: bool = False,
    source: str = "video",
    work_dir: Path | None = None,
    collection_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """处理一个完整音频：从文件到 Telegram 推送。

    Args:
        audio_path: 输入音频路径（任何 ffmpeg 能读的格式）
        settings: 全局配置
        air_date: 节目播出日期字符串（用于消息标题）。默认今天。
        display_name: 覆盖 Telegram 标题与 transcript 标题。默认用配置中的节目名。
        fine_translation: True 时使用配置中的精细翻译模型。
        source: metrics 记录用，标识此次运行的入口类型。
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"音频文件不存在：{audio_path}")

    air_date = air_date or datetime.now().strftime("%Y-%m-%d")
    program_name = display_name or settings.program.name

    metrics = MetricsCollector(
        source=source,
        program_name=program_name,
        air_date=air_date,
        run_id=run_id,
    )
    metrics_path = settings.runtime.logs_dir / "metrics.jsonl"

    if work_dir is None:
        work_dir = build_work_dir(
            settings.runtime.recordings_dir,
            program_name,
            air_date,
            settings.summary.segments_library_path,
            source,
            collection_id=collection_id,
        )
    work_dir.mkdir(parents=True, exist_ok=True)

    success = False
    try:
        # 1. 切片
        with metrics.step("segment_audio"):
            slices = await segment_audio(
                audio_path,
                work_dir / "segments",
                segment_seconds=settings.stt.segment_seconds,
                silence_align=settings.stt.silence_align,
            )

        # 2. 转写（日文，带时间戳）
        with metrics.step("transcribe_segments"):
            ja_segments = await transcribe_segments(
                slices,
                settings,
                program_display_name=program_name,
                source_audio=audio_path,
            )
        if not ja_segments:
            metrics.add_error("转写结果为空")
            logger.warning("转写结果为空，pipeline 中止")
            return
        _dump_segments_to_disk(ja_segments, work_dir / "03_ja_segments.json")
        metrics.metrics.segments_count = len(ja_segments)

        # 3. 翻译
        with metrics.step("translate_segments"):
            bilingual = await translate_segments(
                ja_segments,
                settings,
                fine=fine_translation,
                token_callback=metrics.add_token_usage,
            )
        metrics.metrics.batches_count = (
            (len(ja_segments) + settings.translation.batch_size - 1)
            // settings.translation.batch_size
        )

        # 4. 名词修正
        bilingual = apply_name_corrections(bilingual, settings.name_corrections)
        bilingual = apply_terminology_corrections(
            bilingual,
            load_post_corrections(settings.translation.terminology_path),
        )
        _dump_segments_to_disk(bilingual, work_dir / "04_bilingual_segments.json")
        failed_translations = _count_translation_failures(bilingual)
        if failed_translations:
            ratio = failed_translations / max(len(bilingual), 1)
            msg = f"翻译失败/缺失 {failed_translations}/{len(bilingual)} 段 ({ratio:.1%})"
            if ratio > 0.05:
                metrics.add_warning(msg)
            else:
                logger.warning(msg)

        # 5. 写双语 txt
        txt_path = work_dir / f"{_safe_filename_part(program_name)}_{air_date}.txt"
        write_bilingual_txt(bilingual, txt_path, program_name)

        # 6. 总结 + 高光（注入往期回忆）
        with metrics.step("summarize"):
            summary = await summarize(
                bilingual,
                settings,
                program_name=program_name,
                air_date=air_date,
                token_callback=metrics.add_token_usage,
            )
        dump_summary_to_disk(summary, work_dir, program_name, air_date)
        _dump_json(work_dir / "05_summary.json", summary.model_dump())
        metrics.metrics.sections_count = len(summary.sections)
        metrics.metrics.library_hits = sum(1 for s in summary.sections if s.is_recurring)

        # 6a. 累积往期回忆：把本期 key_topics + highlights 短评 append 到 jsonl
        history_path = settings.runtime.recordings_dir.parent / "history_context.jsonl"
        try:
            append_history_entry(history_path, program_name, air_date, summary)
        except Exception as e:
            logger.warning(f"history append 失败（忽略不阻断）：{e!r}")

        # 6b. 新环节进入 HITL 队列；确认后才写 segments_library。
        pending_segments = []
        if settings.summary.auto_append_new_segments:
            series_name = extract_series_name(program_name)
            new_entries = [
                {"title_ja": s.title_ja, "intro": s.intro}
                for s in summary.sections
                if not s.is_recurring and s.title_ja and s.intro
            ]
            if new_entries:
                store = ApprovalStore(default_approval_store_path(settings.runtime.logs_dir))
                pending_segments = store.add_segments(
                    program_series=series_name,
                    program_name=program_name,
                    air_date=air_date,
                    segments=new_entries,
                    library_path=settings.summary.segments_library_path,
                )
                logger.info(
                    f"新环节待审批：{len(pending_segments)} 个（未直接写入 library）"
                )

        # 7. 推 Telegram
        with metrics.step("send_to_telegram"):
            await send_to_telegram(
                settings,
                summary,
                txt_path,
                program_name,
                air_date,
                pending_segments=pending_segments,
            )
        # 估算 Telegram 消息数 = 1 header + ceil(sections/4) + 0（高光已删）
        metrics.metrics.telegram_messages_sent = (
            1 + max(1, (len(summary.sections) + 3) // 4)
        )

        # 7a. Optional downstream handoff: let radio_kg ingest this episode.
        await _handoff_to_knowledge_base(settings, work_dir)

        elapsed = time.monotonic() - metrics._start
        logger.success(
            f"Pipeline 完成 ✅ 总耗时 {elapsed:.1f}s ({elapsed / 60:.1f} min)"
        )
        success = True

    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        metrics.add_error(err_msg)
        logger.exception(f"Pipeline 异常：{err_msg}")
        # 尽力发个 Telegram 失败通知（自身失败也别二次崩溃）
        try:
            await notify_pipeline_failure(
                settings,
                program_name=program_name,
                air_date=air_date,
                error_message=err_msg,
            )
        except Exception as notify_exc:
            logger.error(f"失败通知本身也发送失败：{notify_exc!r}")
        raise

    finally:
        # 8. 成功才清理音频切片；失败时保留切片和中间 JSON 便于复盘。
        seg_dir = work_dir / "segments"
        if success and seg_dir.exists():
            shutil.rmtree(seg_dir, ignore_errors=True)
            logger.info(f"已清理切片目录；产物保留在 {work_dir}")
        elif seg_dir.exists():
            logger.info(f"pipeline 未成功，保留切片目录便于排查：{seg_dir}")

        # 9. flush metrics
        metrics.finalize(success=success)
        try:
            metrics.flush(metrics_path)
        except Exception as flush_exc:
            logger.error(f"metrics flush 失败：{flush_exc!r}")


def _safe_filename_part(value: str, max_length: int = 80) -> str:
    """把视频标题等外部文本变成安全的文件名片段。"""
    cleaned = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in value).strip()
    return (cleaned or "untitled")[:max_length]


def _dump_segments_to_disk(segments, path: Path) -> Path:
    return _dump_json(path, [seg.model_dump() for seg in segments])


def _dump_json(path: Path, payload) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"中间产物已落盘：{path}")
    return path


def _count_translation_failures(segments) -> int:
    failure_markers = ("[翻译失败]", "[翻译缺失]")
    return sum(1 for seg in segments if any(marker in seg.zh for marker in failure_markers))


async def _handoff_to_knowledge_base(settings: Settings, work_dir: Path) -> None:
    kb = settings.knowledge_base
    ingest_url = (
        os.environ.get("RADIO_KG_AUTO_INGEST_URL")
        or os.environ.get("RADIO_KG_INGEST_URL")
        or kb.ingest_url
    ).strip()
    enabled = _env_bool(
        os.environ.get("RADIO_KG_AUTO_INGEST"),
        kb.enabled or bool(os.environ.get("RADIO_KG_AUTO_INGEST_URL")),
    )
    if not enabled or not ingest_url:
        return

    try:
        async with httpx.AsyncClient(timeout=kb.timeout_seconds) as client:
            resp = await client.post(ingest_url, json={"dir": str(work_dir.resolve())})
            resp.raise_for_status()
            payload = resp.json()
        status = payload.get("status", "unknown")
        thread_id = payload.get("thread_id", "")
        logger.info(f"radio_kg 自动入库已触发：status={status} thread_id={thread_id}")
        if status == "interrupted":
            logger.warning("radio_kg 入库需要人工审批，请到知识库看板处理 pending 项。")
    except Exception as exc:
        msg = f"radio_kg 自动入库触发失败：{type(exc).__name__}: {exc}"
        if kb.fail_pipeline_on_error:
            raise RuntimeError(msg) from exc
        logger.warning(msg)


def _env_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
