"""YouTube live 入口：URL → 定长录制 → 跑完整 pipeline → Telegram。

用法：
    uv run python scripts/main_youtube_live.py "https://www.youtube.com/@example/live" \\
        --duration 60 \\
        --title "节目名"
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datetime import datetime  # noqa: E402

from radio.config import load_settings  # noqa: E402
from radio.pipeline import run_pipeline  # noqa: E402
from radio.recordings_layout import build_work_dir  # noqa: E402
from radio.utils.logging import setup_logging  # noqa: E402
from radio.youtube_live_source import record_youtube_live  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="录制 YouTube Live 并跑完整 pipeline → Telegram")
    p.add_argument("url", help="YouTube 直播地址，如 https://www.youtube.com/@handle/live")
    p.add_argument(
        "--duration",
        type=int,
        required=True,
        help="录制时长（分钟）",
    )
    p.add_argument(
        "--title",
        default=None,
        help="节目展示标题（默认使用 YouTube 标题）",
    )
    p.add_argument(
        "--air-date",
        default=None,
        help="内容日期 YYYY-MM-DD（默认今天）",
    )
    p.add_argument(
        "--detection-timeout-minutes",
        type=int,
        default=30,
        help="等待开播最长时间（分钟，默认 30）",
    )
    p.add_argument(
        "--detection-interval-seconds",
        type=int,
        default=60,
        help="开播探测间隔（秒，默认 60）",
    )
    p.add_argument(
        "--skip-live-detection",
        action="store_true",
        help="跳过 is_live 探测，直接交给 yt-dlp 录制（调试用）",
    )
    p.add_argument(
        "--cookies",
        type=Path,
        default=None,
        help="可选 cookies.txt，用于需要登录态或会员限定的直播",
    )
    p.add_argument(
        "--keep-audio",
        action="store_true",
        help="保留下载/抽取出的音频文件，便于排查",
    )
    p.add_argument(
        "--fine-translation",
        action="store_true",
        help="使用 Claude Haiku 精细翻译，而不是默认 DeepSeek 批量翻译",
    )
    p.add_argument(
        "--config",
        default="config/config.yaml",
        help="YAML 配置文件路径",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    setup_logging(settings.runtime.logs_dir)

    from loguru import logger

    tmp_dir = settings.runtime.recordings_dir / f".tmp_youtube_live_{int(time.time())}"
    work_dir: Path | None = None
    try:
        live_audio = await record_youtube_live(
            args.url,
            tmp_dir,
            duration_minutes=args.duration,
            title=args.title,
            cookies_path=args.cookies,
            detection_timeout_minutes=args.detection_timeout_minutes,
            detection_interval_seconds=args.detection_interval_seconds,
            wait_for_live=not args.skip_live_detection,
        )
        title = args.title or live_audio.title
        air_date = args.air_date or datetime.now().strftime("%Y-%m-%d")
        work_dir = build_work_dir(
            settings.runtime.recordings_dir,
            title,
            air_date,
            settings.summary.segments_library_path,
            source="youtube_live",
        )
        work_dir.mkdir(parents=True, exist_ok=True)

        final_audio = work_dir / live_audio.audio_path.name
        shutil.move(str(live_audio.audio_path), str(final_audio))
        logger.info(f"音频已就位：{final_audio}")

        await run_pipeline(
            final_audio,
            settings,
            air_date=air_date,
            display_name=title,
            fine_translation=args.fine_translation,
            source="youtube_live",
            work_dir=work_dir,
        )
        if not args.keep_audio:
            final_audio.unlink(missing_ok=True)
    except Exception as e:
        logger.exception(f"YouTube live pipeline 失败：{e}")
        return 1
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
