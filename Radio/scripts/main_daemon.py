"""调度器守护进程入口：按 config.yaml 的 scheduled_programs cron 定时录制。

用法：
    uv run python scripts/main_daemon.py
    uv run python scripts/main_daemon.py --config config/config.yaml

后台 / 开机自启：见 deploy/radio.plist（macOS launchd）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from radio.config import load_settings  # noqa: E402
from radio.scheduler import start_scheduler_and_wait  # noqa: E402
from radio.utils.logging import setup_logging  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Radio-Oshikatsu 守护进程（APScheduler）")
    p.add_argument(
        "--config",
        default="config/config.yaml",
        help="YAML 配置文件路径（默认 config/config.yaml）",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    setup_logging(settings.runtime.logs_dir)

    from loguru import logger
    logger.info("🚀 Radio-Oshikatsu daemon 启动")
    logger.info(f"  config: {args.config}")
    logger.info(f"  jobstore: {settings.scheduler.jobstore_path}")
    logger.info(f"  scheduled_programs: {len(settings.scheduled_programs)}")

    try:
        await start_scheduler_and_wait(settings, str(args.config))
    except (KeyboardInterrupt, SystemExit):
        logger.info("接收到中断信号，退出守护进程")
        return 0
    except Exception as e:
        logger.exception(f"守护进程异常：{e!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
