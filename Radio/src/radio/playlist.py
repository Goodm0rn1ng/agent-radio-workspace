"""Playlist expansion helpers for frontend/API batch jobs."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class PlaylistItem:
    """One concrete video selected from a playlist range."""

    index: int
    url: str
    title: str


async def expand_playlist_range(
    playlist_url: str,
    *,
    start_index: int,
    end_index: int,
    cookies_path: Path | None = None,
) -> list[PlaylistItem]:
    """Expand a playlist URL to concrete video URLs between two indices.

    The returned order follows the requested direction, so `178 -> 1` yields
    index 178, 177, ..., 1.
    """
    return await asyncio.to_thread(
        _expand_playlist_range_sync,
        playlist_url,
        start_index,
        end_index,
        cookies_path,
    )


def _expand_playlist_range_sync(
    playlist_url: str,
    start_index: int,
    end_index: int,
    cookies_path: Path | None,
) -> list[PlaylistItem]:
    if start_index <= 0 or end_index <= 0:
        raise ValueError("playlist index 必须大于 0")
    if cookies_path is not None and not cookies_path.exists():
        raise FileNotFoundError(f"cookies 文件不存在：{cookies_path}")

    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise RuntimeError("yt-dlp 未安装，请先运行 `uv sync`") from e

    ydl_opts: dict[str, Any] = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
    }
    if cookies_path is not None:
        ydl_opts["cookiefile"] = str(cookies_path)

    with YoutubeDL(ydl_opts) as ydl:
        raw = ydl.extract_info(playlist_url, download=False)
    if raw is None:
        raise RuntimeError("yt-dlp 没有返回播放列表信息")

    entries = [entry for entry in (raw.get("entries") or []) if isinstance(entry, dict)]
    ordered = _items_from_entries(
        entries,
        playlist_url=playlist_url,
        start_index=start_index,
        end_index=end_index,
    )
    if not ordered:
        raise ValueError(
            f"播放列表范围没有匹配条目：start_index={start_index}, end_index={end_index}"
        )
    return ordered


def _items_from_entries(
    entries: list[dict[str, Any]],
    *,
    playlist_url: str,
    start_index: int,
    end_index: int,
) -> list[PlaylistItem]:
    title_indexed = _items_from_episode_titles(
        entries,
        start_index=start_index,
        end_index=end_index,
    )
    if title_indexed:
        return title_indexed

    anchor = _anchor_from_url(playlist_url)
    if anchor is not None:
        anchor_video_id, anchor_index = anchor
        anchor_ordinal = _find_entry_ordinal(entries, anchor_video_id)
        if anchor_ordinal is not None:
            return _items_from_anchor(
                entries,
                start_index=start_index,
                end_index=end_index,
                anchor_index=anchor_index,
                anchor_ordinal=anchor_ordinal,
            )

    selected: dict[int, PlaylistItem] = {}
    lo, hi = sorted((start_index, end_index))
    for ordinal, entry in enumerate(entries, start=1):
        index = int(entry.get("playlist_index") or ordinal)
        if index < lo or index > hi:
            continue
        selected[index] = _playlist_item(entry, index)

    step = -1 if start_index > end_index else 1
    return [
        selected[index]
        for index in range(start_index, end_index + step, step)
        if index in selected
    ]


def _items_from_episode_titles(
    entries: list[dict[str, Any]],
    *,
    start_index: int,
    end_index: int,
) -> list[PlaylistItem]:
    episode_numbers = [
        episode_number
        for entry in entries
        if (episode_number := _episode_number_from_title(str(entry.get("title") or "")))
        is not None
    ]
    if len(episode_numbers) < 3:
        return []

    max_episode = max(episode_numbers)
    selected: dict[int, PlaylistItem] = {}
    lo, hi = sorted((start_index, end_index))
    for entry in entries:
        episode_number = _episode_number_from_title(str(entry.get("title") or ""))
        if episode_number is None:
            continue
        inferred_index = max_episode + 1 - episode_number
        if inferred_index < lo or inferred_index > hi:
            continue
        selected[inferred_index] = _playlist_item(entry, inferred_index)

    step = -1 if start_index > end_index else 1
    ordered = [
        selected[index]
        for index in range(start_index, end_index + step, step)
        if index in selected
    ]
    return ordered if len(ordered) == abs(start_index - end_index) + 1 else []


def _episode_number_from_title(title: str) -> int | None:
    match = re.search(r"#\s*(\d+)(?!\d)", title)
    if not match:
        return None
    return int(match.group(1))


def _items_from_anchor(
    entries: list[dict[str, Any]],
    *,
    start_index: int,
    end_index: int,
    anchor_index: int,
    anchor_ordinal: int,
) -> list[PlaylistItem]:
    step = -1 if start_index > end_index else 1
    items: list[PlaylistItem] = []
    for requested_index in range(start_index, end_index + step, step):
        ordinal = anchor_ordinal + requested_index - anchor_index
        if ordinal < 1 or ordinal > len(entries):
            continue
        items.append(_playlist_item(entries[ordinal - 1], requested_index))
    return items


def _playlist_item(entry: dict[str, Any], index: int) -> PlaylistItem:
    title = str(entry.get("title") or entry.get("id") or f"playlist item {index}")
    return PlaylistItem(
        index=index,
        url=_entry_url(entry),
        title=title,
    )


def _anchor_from_url(url: str) -> tuple[str, int] | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    video_id = (query.get("v") or [""])[0]
    index_value = (query.get("index") or [""])[0]

    if not video_id and parsed.netloc.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/", 1)[0]
    if not video_id or not index_value:
        return None

    try:
        index = int(index_value)
    except ValueError:
        return None
    if index <= 0:
        return None
    return video_id, index


def _find_entry_ordinal(entries: list[dict[str, Any]], video_id: str) -> int | None:
    for ordinal, entry in enumerate(entries, start=1):
        entry_id = str(entry.get("id") or "")
        entry_url = str(entry.get("url") or entry.get("webpage_url") or "")
        if entry_id == video_id or f"v={video_id}" in entry_url or entry_url.endswith(f"/{video_id}"):
            return ordinal
    return None


def _entry_url(entry: dict[str, Any]) -> str:
    webpage_url = entry.get("webpage_url")
    if webpage_url:
        return str(webpage_url)

    url = str(entry.get("url") or "")
    if url.startswith("http://") or url.startswith("https://"):
        return url

    video_id = str(entry.get("id") or url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    raise ValueError(f"播放列表条目缺少 URL：{entry}")
