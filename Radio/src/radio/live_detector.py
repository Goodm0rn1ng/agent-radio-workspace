"""Live stream detection helpers.

Currently used for YouTube channel `/live` URLs before starting a scheduled
recording. The detector is deliberately small: it polls yt-dlp metadata until
the stream reports `live_status=is_live`, then returns the concrete watch URL
and title for the recorder/pipeline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class YouTubeLiveInfo:
    """Minimal metadata needed to start a YouTube live recording."""

    input_url: str
    webpage_url: str
    title: str
    live_status: str

    @property
    def is_live(self) -> bool:
        return self.live_status == "is_live"


async def wait_for_youtube_live(
    url: str,
    *,
    timeout_minutes: int = 30,
    interval_seconds: int = 60,
    cookies_path: Path | None = None,
) -> YouTubeLiveInfo:
    """Poll yt-dlp until a YouTube URL resolves to an active live stream."""
    timeout_s = max(0, timeout_minutes) * 60
    interval_s = max(5, interval_seconds)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    last_status = "unknown"
    last_error = ""

    while True:
        try:
            info = await asyncio.to_thread(_extract_youtube_live_info, url, cookies_path)
            last_status = info.live_status
            if info.is_live:
                logger.success(f"YouTube live detected: {info.title} ({info.webpage_url})")
                return info
            logger.info(
                f"YouTube live not active yet: status={info.live_status}, title={info.title}"
            )
        except Exception as e:
            last_error = repr(e)
            logger.warning(f"YouTube live detection failed: {last_error}")

        remaining = deadline - loop.time()
        if remaining <= 0:
            detail = f"last_status={last_status}"
            if last_error:
                detail += f", last_error={last_error}"
            raise TimeoutError(f"YouTube live 未在 {timeout_minutes} 分钟内开播（{detail}）")
        await asyncio.sleep(min(interval_s, remaining))


def probe_youtube_live(
    url: str,
    *,
    cookies_path: Path | None = None,
) -> YouTubeLiveInfo:
    """Return the current yt-dlp live metadata once, without waiting."""
    return _extract_youtube_live_info(url, cookies_path)


def _extract_youtube_live_info(
    url: str,
    cookies_path: Path | None,
) -> YouTubeLiveInfo:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise RuntimeError("yt-dlp 未安装，请先运行 `uv sync`") from e

    if cookies_path is not None and not cookies_path.exists():
        raise FileNotFoundError(f"cookies 文件不存在：{cookies_path}")

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if cookies_path is not None:
        ydl_opts["cookiefile"] = str(cookies_path)

    with YoutubeDL(ydl_opts) as ydl:
        raw = ydl.extract_info(url, download=False)
    if raw is None:
        raise RuntimeError("yt-dlp 没有返回直播信息")
    info = _first_entry(raw)

    live_status = str(info.get("live_status") or "")
    if not live_status and info.get("is_live"):
        live_status = "is_live"
    title = str(info.get("title") or info.get("id") or "YouTube Live")
    webpage_url = str(info.get("webpage_url") or info.get("original_url") or url)
    return YouTubeLiveInfo(
        input_url=url,
        webpage_url=webpage_url,
        title=title,
        live_status=live_status or "unknown",
    )


def _first_entry(info: dict[str, Any]) -> dict[str, Any]:
    """Flatten yt-dlp playlist/channel responses to the first concrete entry."""
    entries = info.get("entries")
    if isinstance(entries, Iterable) and not isinstance(entries, (str, bytes, dict)):
        for entry in entries:
            if isinstance(entry, dict):
                return entry
    return info
