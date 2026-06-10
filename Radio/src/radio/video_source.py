"""已有视频输入：用 yt-dlp 下载/抽取音频，交给通用音频 pipeline。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from radio.utils.ffmpeg import find_ffmpeg

_PLAY_STORES_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
}


@dataclass(frozen=True)
class VideoAudio:
    """已抽取好的视频音频及少量元信息。"""

    audio_path: Path
    title: str
    source_url: str


async def extract_audio_from_video_url(
    url: str,
    output_dir: Path,
    *,
    cookies_path: Path | None = None,
    audio_format: str = "m4a",
) -> VideoAudio:
    """从 Bili/YouTube 等 yt-dlp 支持的视频 URL 抽取音频。"""
    return await asyncio.to_thread(
        _extract_audio_sync,
        url,
        output_dir,
        cookies_path,
        audio_format,
    )


def _extract_audio_sync(
    url: str,
    output_dir: Path,
    cookies_path: Path | None,
    audio_format: str,
) -> VideoAudio:
    if _is_play_stores_video_url(url):
        return _extract_play_stores_audio_sync(url, output_dir, audio_format)

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg 不在 PATH 中，请 `brew install ffmpeg`")

    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise RuntimeError("yt-dlp 未安装，请先运行 `uv sync`") from e

    if cookies_path is not None and not cookies_path.exists():
        raise FileNotFoundError(f"cookies 文件不存在：{cookies_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"开始抽取视频音频：{url}")

    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "outtmpl": str(output_dir / "source.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            }
        ],
        "quiet": True,
    }
    if cookies_path is not None:
        ydl_opts["cookiefile"] = str(cookies_path)

    before = {p.resolve() for p in output_dir.glob("*") if p.is_file()}
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info is None:
        raise RuntimeError("yt-dlp 没有返回视频信息")

    if "entries" in info:
        entries = [entry for entry in info["entries"] if entry]
        if entries:
            info = entries[0]

    audio_path = _find_downloaded_audio(output_dir, before, audio_format)
    title = str(info.get("title") or info.get("id") or "video")
    source_url = str(info.get("webpage_url") or url)
    logger.info(f"视频音频已准备：{audio_path.name}（{title}）")
    return VideoAudio(audio_path=audio_path, title=title, source_url=source_url)


def _is_play_stores_video_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.hostname.endswith(".stores.play.jp")
        and re.search(r"/videos/[^/?#]+", parsed.path) is not None
    )


def _extract_play_stores_audio_sync(
    url: str,
    output_dir: Path,
    audio_format: str,
) -> VideoAudio:
    ffmpeg = find_ffmpeg()
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"开始抽取 PLAY VIDEO STORES 音频：{url}")

    with httpx.Client(headers=_PLAY_STORES_HEADERS, follow_redirects=True, timeout=30) as client:
        page = client.get(url)
        page.raise_for_status()
        media = _parse_play_stores_media(page.text, url)
        playback = client.post(
            f"{media.origin}/api/streaks/playback/fetch",
            json={"refId": media.ref_id},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": media.origin,
                "Referer": url,
            },
        )
        playback.raise_for_status()

    source_url = _select_play_stores_hls(playback.json())
    audio_path = output_dir / f"source.{audio_format}"
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel",
        "warning",
        "-i",
        source_url,
        "-vn",
        "-c:a",
        "copy",
        str(audio_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg 抽取 PLAY VIDEO STORES 音频失败："
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    if not audio_path.exists() or audio_path.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg 没产生可用音频文件：{audio_path}")

    logger.info(f"PLAY VIDEO STORES 音频已准备：{audio_path.name}（{media.title}）")
    return VideoAudio(audio_path=audio_path, title=media.title, source_url=url)


@dataclass(frozen=True)
class _PlayStoresMedia:
    title: str
    ref_id: str
    origin: str


def _parse_play_stores_media(html: str, url: str) -> _PlayStoresMedia:
    parsed = urlparse(url)
    content_id_match = re.search(r"/videos/([^/?#]+)", parsed.path)
    if content_id_match is None:
        raise ValueError(f"PLAY VIDEO STORES URL に content id がありません：{url}")
    content_id = content_id_match.group(1)

    app_match = re.search(r"window\.app=(\{.*?\});</script>", html, flags=re.S)
    if app_match is None:
        raise ValueError("PLAY VIDEO STORES ページから app state を取得できません")
    app_state = json.loads(app_match.group(1))
    cache = app_state.get("falcorCache", {})
    content_media = cache["query"]["medias"]["byContentId"][content_id]
    media_id = content_media["0"]["value"][1]
    media = cache["media"][media_id]
    ref_id = media["refId"]["value"]
    title = media["name"]["value"]
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return _PlayStoresMedia(title=title, ref_id=ref_id, origin=origin)


def _select_play_stores_hls(playback: dict[str, Any]) -> str:
    sources = playback.get("sources")
    if not isinstance(sources, list):
        raise ValueError("PLAY VIDEO STORES playback 响应缺少 sources")
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_type = str(source.get("type") or "")
        source_url = str(source.get("src") or "")
        if source_url and (
            "mpegURL" in source_type or source_url.split("?", 1)[0].endswith(".m3u8")
        ):
            return source_url
    raise ValueError("PLAY VIDEO STORES playback 响应没有可用的 HLS source")


def _find_downloaded_audio(
    output_dir: Path,
    before: set[Path],
    audio_format: str,
) -> Path:
    expected = output_dir / f"source.{audio_format}"
    if expected.exists():
        return expected

    new_files = [
        p
        for p in output_dir.glob("*")
        if p.is_file() and p.resolve() not in before
    ]
    preferred = [
        p for p in new_files if p.suffix.lower().lstrip(".") == audio_format.lower()
    ]
    candidates = preferred or new_files
    if not candidates:
        raise FileNotFoundError(f"未找到抽取后的音频文件：{output_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)
