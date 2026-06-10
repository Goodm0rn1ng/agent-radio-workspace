"""从已有的 bilingual.txt 重做 summarize + Telegram 推送，跳过 STT/翻译。

适用于：summarize / telegram 阶段失败、想换 prompt 或 provider 重跑。

用法：
    uv run python scripts/main_resummarize.py \\
        data/recordings/work_XXX/节目名_2026-05-13.txt \\
        --title "MyGO!!!!!の「迷子集会」#178" \\
        --air-date 2026-05-13
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from radio.approval import ApprovalStore, default_approval_store_path  # noqa: E402
from radio.config import load_settings  # noqa: E402
from radio.models import Segment  # noqa: E402
from radio.profiles import apply_prompt_profile, load_prompt_profile  # noqa: E402
from radio.segments_library import extract_series_name  # noqa: E402
from radio.summarize import dump_summary_to_disk, summarize  # noqa: E402
from radio.telegram_sender import send_to_telegram  # noqa: E402
from radio.terminology import apply_summary_corrections, load_post_corrections  # noqa: E402
from radio.utils.logging import setup_logging  # noqa: E402

TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*$")


def parse_bilingual_txt(path: Path) -> list[Segment]:
    """解析 transcript.write_bilingual_txt 写出的 .txt 文件。

    文件格式：
        # 节目名
        # 共 N 段，总时长约 HH:MM:SS

        [HH:MM:SS]
          JP: ...
          CN: ...

        [HH:MM:SS]
        ...
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    segments: list[Segment] = []
    current_start: float | None = None
    current_ja: str | None = None
    current_zh: str | None = None

    def _flush() -> None:
        if current_start is None or current_ja is None:
            return
        segments.append(
            Segment(
                i=len(segments),
                start=current_start,
                end=current_start,  # txt 里没存 end，用 start 占位（不影响 summarize）
                ja=current_ja,
                zh=current_zh or "",
            )
        )

    for ln in lines:
        ts_match = TS_RE.match(ln)
        if ts_match:
            _flush()
            h, m, s = (int(g) for g in ts_match.groups())
            current_start = h * 3600 + m * 60 + s
            current_ja = None
            current_zh = None
        elif ln.lstrip().startswith("JP:"):
            current_ja = ln.split("JP:", 1)[1].strip()
        elif ln.lstrip().startswith("CN:"):
            current_zh = ln.split("CN:", 1)[1].strip()
    _flush()
    return segments


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="从已有 bilingual.txt 重做 summary + Telegram")
    p.add_argument("txt", type=Path, help="bilingual.txt 路径")
    p.add_argument("--title", required=True, help="节目标题（用于 Telegram 消息）")
    p.add_argument("--air-date", required=True, help="节目播出日期 YYYY-MM-DD")
    p.add_argument("--config", default="config/config.yaml", help="YAML 配置")
    p.add_argument("--profile", default=None, help="Prompt profile id")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    if args.profile:
        settings = apply_prompt_profile(settings, load_prompt_profile(args.profile))
    setup_logging(settings.runtime.logs_dir)

    from loguru import logger

    if not args.txt.exists():
        logger.error(f"transcript 文件不存在：{args.txt}")
        return 1

    logger.info(f"读取 transcript：{args.txt}")
    segments = parse_bilingual_txt(args.txt)
    logger.info(f"重建 segments：{len(segments)} 段")
    if not segments:
        logger.error("没有解析出任何 segment，请检查 txt 文件")
        return 1

    try:
        summary = await summarize(
            segments,
            settings,
            program_name=args.title,
            air_date=args.air_date,
        )
        summary = apply_summary_corrections(
            summary,
            load_post_corrections(settings.translation.terminology_path),
        )
        dump_summary_to_disk(summary, args.txt.parent, args.title, args.air_date)

        pending_segments = []
        if settings.summary.auto_append_new_segments:
            series_name = extract_series_name(args.title)
            new_entries = [
                {"title_ja": s.title_ja, "intro": s.intro}
                for s in summary.sections
                if not s.is_recurring and s.title_ja and s.intro
            ]
            if new_entries:
                store = ApprovalStore(default_approval_store_path(settings.runtime.logs_dir))
                pending_segments = store.add_segments(
                    program_series=series_name,
                    program_name=args.title,
                    air_date=args.air_date,
                    segments=new_entries,
                    library_path=settings.summary.segments_library_path,
                )
                logger.info(f"新环节待审批：{len(pending_segments)} 个")

        await send_to_telegram(
            settings,
            summary,
            args.txt,
            args.title,
            args.air_date,
            pending_segments=pending_segments,
        )
        logger.success("重做完成 ✅")
    except Exception as e:
        logger.exception(f"重做失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
