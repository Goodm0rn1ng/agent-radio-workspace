"""由 episode_label 反查 Radio recordings 里的源媒体文件。

episode_label（如 "#3 こもればなし"）由 doc_agent.parse_folder_metadata 从文件夹名
派生，这里反向建一张 {episode_label: folder} 表，再在该文件夹找媒体文件。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from config.settings import settings
from src.agents.doc_agent import parse_folder_metadata
from src.source_data import iter_episode_folders

_MEDIA_EXTS = (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".wav", ".aac", ".opus", ".ts", ".flv")
# 视频优先（Branch B/带画面），其次音频（过往广播）。
_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".ts", ".flv")


@lru_cache(maxsize=1)
def _label_to_folder() -> dict[str, Path]:
    data_dir = settings.abspath(settings.radio_data_dir)
    out: dict[str, Path] = {}
    for folder in iter_episode_folders(
        data_dir, archives_only=False, require_segments=False, require_number=False
    ):
        label = parse_folder_metadata(str(folder), settings.program_name).episode_label
        out.setdefault(label, folder)
    return out


def find_media(episode_label: str) -> Path | None:
    """返回该期的源媒体路径（视频优先），找不到返回 None。"""
    folder = _label_to_folder().get(episode_label)
    if folder is None:
        return None
    return media_in_folder(folder)


def media_in_folder(folder: Path) -> Path | None:
    candidates = [p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in _MEDIA_EXTS]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.suffix.lower() not in _VIDEO_EXTS, -p.stat().st_size))
    return candidates[0]


def has_video(path: Path | str) -> bool:
    return Path(path).suffix.lower() in _VIDEO_EXTS
