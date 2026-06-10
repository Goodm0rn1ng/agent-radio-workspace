"""已有视频入口：Bili/YouTube 等视频 URL → 抽音频 → 跑完整 pipeline。

用法：
    uv run python scripts/main_video.py "https://www.bilibili.com/video/BV..."
    uv run python scripts/main_video.py URL --cookies path/to/cookies.txt --title "视频标题"
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

# 把 src/ 加入 import 路径（这样不需要 pip install -e .）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from radio.config import load_settings  # noqa: E402
from radio.pipeline import run_pipeline  # noqa: E402
from radio.profiles import apply_prompt_profile, load_prompt_profile  # noqa: E402
from radio.recordings_layout import build_work_dir  # noqa: E402
from radio.utils.logging import setup_logging  # noqa: E402
from radio.video_source import extract_audio_from_video_url  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="跑一次已有视频 URL → Telegram pipeline")
    p.add_argument("url", help="Bili/YouTube 等 yt-dlp 支持的视频 URL")
    p.add_argument(
        "--air-date",
        default=None,
        help="内容日期 YYYY-MM-DD（默认今天）",
    )
    p.add_argument(
        "--title",
        default=None,
        help="覆盖 Telegram 和 transcript 中使用的标题（默认使用视频标题）",
    )
    p.add_argument(
        "--cookies",
        type=Path,
        default=None,
        help="可选 cookies.txt，用于需要登录态的视频",
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
    p.add_argument(
        "--profile",
        default=None,
        help="Prompt profile id（如 mygo_meigo_shukai / hina_radio）",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    if args.profile:
        settings = apply_prompt_profile(settings, load_prompt_profile(args.profile))
    setup_logging(settings.runtime.logs_dir)

    from datetime import datetime

    from loguru import logger

    # 第一步用 tmp 目录抽音频（因为 yt-dlp 抽完才知道真实视频标题）
    tmp_dir = settings.runtime.recordings_dir / f".tmp_video_{int(time.time())}"
    work_dir: Path | None = None
    try:
        video_audio = await extract_audio_from_video_url(
            args.url,
            tmp_dir,
            cookies_path=args.cookies,
        )
        # 拿到标题 + air_date 后算最终 work_dir
        title = args.title or video_audio.title
        air_date = args.air_date or datetime.now().strftime("%Y-%m-%d")
        work_dir = build_work_dir(
            settings.runtime.recordings_dir,
            title,
            air_date,
            settings.summary.segments_library_path,
            source="video",
            collection_id=args.profile,
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        # 把抽出的音频从 tmp 挪进 work_dir
        final_audio = work_dir / video_audio.audio_path.name
        shutil.move(str(video_audio.audio_path), str(final_audio))
        logger.info(f"音频已就位：{final_audio}")

        await run_pipeline(
            final_audio,
            settings,
            air_date=air_date,
            display_name=title,
            fine_translation=args.fine_translation,
            source="video",
            work_dir=work_dir,
            collection_id=args.profile,
        )
        # 跑完决定要不要删音频
        if not args.keep_audio:
            final_audio.unlink(missing_ok=True)
    except Exception as e:
        logger.exception(f"视频 pipeline 失败：{e}")
        return 1
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
