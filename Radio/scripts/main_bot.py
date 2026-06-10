"""Run Telegram polling bot for HITL approvals."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from radio.config import load_settings  # noqa: E402
from radio.telegram_bot import run_bot  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Radio-Oshikatsu Telegram HITL bot")
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml",
    )
    args = parser.parse_args()

    run_bot(load_settings(args.config))


if __name__ == "__main__":
    main()
