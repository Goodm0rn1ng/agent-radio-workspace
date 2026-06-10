"""日志配置：控制台彩色 + 文件按天轮转，保留 14 天。"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(logs_dir: Path = Path("data/logs"), level: str = "INFO") -> None:
    """初始化全局 logger。在程序入口调用一次。"""
    logs_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()  # 清掉默认 stderr handler

    # 控制台：彩色，紧凑格式
    logger.add(
        sys.stderr,
        level=level,
        format=(
            "<green>{time:HH:mm:ss}</green> "
            "<level>{level: <7}</level> "
            "<cyan>{module}</cyan> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 文件：按天轮转，保留 14 天
    logger.add(
        logs_dir / "radio_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {module}:{function}:{line} | {message}",
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
    )
