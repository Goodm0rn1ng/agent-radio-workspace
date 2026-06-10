"""Radiko time-shift 入口：URL → 录制 → 跑完整 pipeline → Telegram。

用法：
    uv run python scripts/main_radiko.py 'https://radiko.jp/#!/ts/QRR/20260511003000' \\
        --duration 30 \\
        --title "重生（文化放送 30 分钟）" \\
        --air-date 2026-05-11

真实 Chrome CDP 模式：
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\
        --remote-debugging-port=9222
    uv run python scripts/main_radiko.py 'https://radiko.jp/#!/ts/QRR/20260511003000' \\
        --duration 30 \\
        --cdp-url http://127.0.0.1:9222
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
from radio.health import (  # noqa: E402
    health_check_before_record,
    notify_health_failure,
)
from radio.pipeline import run_pipeline  # noqa: E402
from radio.radiko_playwright_source import record_radiko_via_playwright  # noqa: E402
from radio.radiko_source import (  # noqa: E402
    parse_radiko_url,
    record_radiko_live,
    record_radiko_timefree,
)
from radio.recordings_layout import build_work_dir  # noqa: E402
from radio.utils.logging import setup_logging  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="录制 Radiko time-shift 节目并跑完整 pipeline → Telegram"
    )
    p.add_argument(
        "url",
        help="Radiko time-shift URL，如 https://radiko.jp/#!/ts/QRR/YYYYMMDDhhmmss",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=60,
        help="节目时长（分钟，默认 60）。Radiko URL 不含结束时间，需手动指定。",
    )
    p.add_argument(
        "--title",
        default=None,
        help="节目展示标题（默认 'Radiko {station} {datetime}'）",
    )
    p.add_argument(
        "--air-date",
        default=None,
        help="节目播出日期 YYYY-MM-DD（默认从 URL 中的开播时间推算）",
    )
    p.add_argument(
        "--fine-translation",
        action="store_true",
        help="使用 Claude Haiku 精细翻译，而不是默认 DeepSeek 批量翻译",
    )
    p.add_argument(
        "--keep-audio",
        action="store_true",
        help="保留下载的 m4a 音频文件（默认跑完删除）",
    )
    p.add_argument(
        "--no-playwright",
        action="store_true",
        help="不用 Playwright（直接 httpx + ffmpeg）。仅做调试用——Radiko 2026 反爬挡住，会失败。",
    )
    p.add_argument(
        "--no-headless",
        action="store_true",
        help="Playwright 可见浏览器（调试用，看页面是否真的加载播放）",
    )
    p.add_argument(
        "--cookies",
        type=Path,
        default=None,
        help="从真实浏览器导出的 cookies JSON 文件（EditThisCookie / Cookie-Editor 格式）。"
             "通常优先用 --cdp-url；旧 radiko_session 可能导致服务端判登录无效。",
    )
    p.add_argument(
        "--cdp-url",
        default=None,
        help="连接已用 --remote-debugging-port 启动的真实 Chrome，例如 http://127.0.0.1:9222。",
    )
    p.add_argument(
        "--skip-health-check",
        action="store_true",
        help="跳过 Radiko 录制前健康检查（默认开）",
    )
    p.add_argument(
        "--health-pre-record-minutes",
        type=int,
        default=0,
        help="健康检查在录制前 N 分钟做并等待（0 = 立即检查，默认）。"
             "适合调度器场景；脚本即时启动用 0。",
    )
    p.add_argument(
        "--fail-on-health-fail",
        action="store_true",
        help="健康检查失败时直接退出（默认仅告警继续尝试录制）",
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

    # 先解析 URL 拿到 station + (ft|live) → 预算 air_date / 标题 → 算 work_dir
    spec = parse_radiko_url(args.url, args.duration)
    if spec.is_live:
        air_date = args.air_date or datetime.now().date().isoformat()
        title = args.title or f"Radiko {spec.station_id} LIVE {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    else:
        air_date = args.air_date or datetime.strptime(spec.ft, "%Y%m%d%H%M%S").date().isoformat()
        title = args.title or f"Radiko {spec.station_id} {spec.ft}"

    work_dir = build_work_dir(
        settings.runtime.recordings_dir,
        title,
        air_date,
        settings.summary.segments_library_path,
        source="radiko",
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"work_dir: {work_dir}")

    # 录制前健康检查
    if not args.skip_health_check:
        health = await health_check_before_record(
            spec.station_id,
            pre_record_minutes=args.health_pre_record_minutes,
            do_wait=args.health_pre_record_minutes > 0,
        )
        if health.ok:
            logger.success(
                f"✓ 健康检查通过：{health.station_id} 当前在播「{health.current_program_title}」"
                f"（{health.current_ft} - {health.current_to}）"
            )
        else:
            logger.error(f"✗ 健康检查失败：{health.detail}")
            try:
                await notify_health_failure(settings, health, program_name=title)
            except Exception as e:
                logger.error(f"health 告警发送失败（忽略）：{e!r}")
            if args.fail_on_health_fail:
                logger.error("--fail-on-health-fail 启用，放弃录制")
                return 1
            logger.warning("继续尝试录制（如需 fail-fast，传 --fail-on-health-fail）")

    try:
        if spec.is_live:
            # Live 端点不被反爬挡，直接用纯 httpx 录制（不需要 Playwright）
            logger.info("使用纯 httpx 路径录制 live")
            radiko_audio = await record_radiko_live(
                args.url,
                work_dir,
                duration_minutes=args.duration,
                title=title,
            )
        elif args.no_playwright:
            radiko_audio = await record_radiko_timefree(
                args.url,
                work_dir,
                duration_minutes=args.duration,
                title=title,
            )
        else:
            radiko_audio = await record_radiko_via_playwright(
                args.url,
                work_dir,
                duration_minutes=args.duration,
                title=title,
                headless=not args.no_headless,
                cookies_path=args.cookies,
                cdp_url=args.cdp_url,
            )
        await run_pipeline(
            radiko_audio.audio_path,
            settings,
            air_date=air_date,
            display_name=title,
            fine_translation=args.fine_translation,
            source="radiko",
            work_dir=work_dir,
        )
        # 跑完根据 --keep-audio 决定是否删音频
        if not args.keep_audio:
            radiko_audio.audio_path.unlink(missing_ok=True)
    except Exception as e:
        logger.exception(f"Radiko pipeline 失败：{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
