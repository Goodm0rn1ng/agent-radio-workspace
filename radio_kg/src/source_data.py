"""Shared discovery helpers for radio artifact folders.

The producer project writes episodes as:
  data/recordings/<collection>/<episode>/

Older curated data is stored flat as:
  hina_radio/<episode>/
"""
from __future__ import annotations

import re
from pathlib import Path

EP_RE = re.compile(r"#(\d+)")
ARCHIVE_MARKER = "アーカイブ"
SEGMENT_FILES = ("04_bilingual_segments.json", "03_ja_segments.json")
SUMMARY_FILE = "05_summary.json"


def episode_number(folder: Path | str) -> int | None:
    match = EP_RE.search(Path(folder).name)
    return int(match.group(1)) if match else None


def has_segments(folder: Path) -> bool:
    return any((folder / name).exists() for name in SEGMENT_FILES)


def is_episode_folder(
    folder: Path,
    *,
    archives_only: bool = False,
    require_segments: bool = False,
    require_summary: bool = False,
    require_number: bool = True,
) -> bool:
    if not folder.is_dir():
        return False
    if require_number and episode_number(folder) is None:
        return False
    if archives_only and ARCHIVE_MARKER not in folder.name:
        return False
    if require_segments and not has_segments(folder):
        return False
    if require_summary and not (folder / SUMMARY_FILE).exists():
        return False
    return True


def iter_episode_folders(
    data_dir: Path,
    *,
    archives_only: bool = False,
    require_segments: bool = False,
    require_summary: bool = False,
    require_number: bool = True,
) -> list[Path]:
    """Return episode folders from both flat and collection-nested layouts."""
    if not data_dir.exists():
        return []

    folders = []
    for path in data_dir.rglob("*"):
        if any(part.startswith(".") for part in path.relative_to(data_dir).parts):
            continue  # skip hidden / .tmp working dirs
        if is_episode_folder(
            path,
            archives_only=archives_only,
            require_segments=require_segments,
            require_summary=require_summary,
            require_number=require_number,
        ):
            folders.append(path)
    return sorted(folders)


def collection_name(folder: Path, data_dir: Path) -> str:
    """The collection a folder belongs to: the first path part under data_dir.

    Flat layout (episode directly under data_dir) reports the program name.
    """
    try:
        rel = folder.relative_to(data_dir)
    except ValueError:
        return folder.parent.name
    return rel.parts[0] if len(rel.parts) > 1 else "(flat)"


def iter_collections(
    data_dir: Path,
    *,
    require_segments: bool = True,
) -> dict[str, list[Path]]:
    """Group every folder carrying broadcast artifacts by its collection.

    No episode-number or archive requirement — un-numbered live recordings and
    differently-titled folders are included so the console can list them all.
    """
    groups: dict[str, list[Path]] = {}
    for folder in iter_episode_folders(
        data_dir, require_segments=require_segments, require_number=False
    ):
        groups.setdefault(collection_name(folder, data_dir), []).append(folder)
    return groups


def select_episode(
    folders: list[Path],
    episode: int,
    *,
    prefer_archives: bool = True,
) -> list[Path]:
    found = [folder for folder in folders if episode_number(folder) == episode]
    if prefer_archives:
        archives = [folder for folder in found if ARCHIVE_MARKER in folder.name]
        if archives:
            return archives
    return found
