"""M1 入口：手动喂一个音频文件，跑完整 pipeline 到 Telegram。

用法：
    uv run python scripts/main_oneshot.py path/to/audio.mp3 [--air-date 2026-05-15]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 把 src/ 加入 import 路径（这样不需要 pip install -e .）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from radio.config import load_settings  # noqa: E402
from radio.pipeline import run_pipeline  # noqa: E402
from radio.profiles import apply_prompt_profile, load_prompt_profile  # noqa: E402
from radio.utils.logging import setup_logging  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="跑一次离线音频 → Telegram pipeline")
    p.add_argument("audio", type=Path, help="本地音频文件路径")
    p.add_argument(
        "--air-date",
        default=None,
        help="节目播出日期 YYYY-MM-DD（默认今天）",
    )
    p.add_argument(
        "--title",
        default=None,
        help="节目标题（影响 recordings/ 子目录归类和 Telegram 推送标题）",
    )
    p.add_argument(
        "--config",
        default="config/config.yaml",
        help="YAML 配置文件路径",
    )
    p.add_argument(
        "--fine-translation",
        action="store_true",
        help="使用 Claude Haiku 精细翻译，而不是默认 DeepSeek 批量翻译",
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

    try:
        await run_pipeline(
            args.audio,
            settings,
            air_date=args.air_date,
            display_name=args.title,
            fine_translation=args.fine_translation,
            source="oneshot",
            collection_id=args.profile,
        )
    except Exception as e:
        from loguru import logger

        logger.exception(f"Pipeline 失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
