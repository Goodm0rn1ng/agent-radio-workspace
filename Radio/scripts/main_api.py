"""Local API server for the future frontend.

Usage:
    uv run python scripts/main_api.py --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import uvicorn  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Radio-Oshikatsu local API server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--config", default="config/config.yaml")
    p.add_argument("--reload", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    os.environ["RADIO_CONFIG"] = args.config
    uvicorn.run(
        "radio.api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(ROOT / "src"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
