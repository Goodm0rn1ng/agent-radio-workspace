"""Branch B：用 yt-dlp 下载 YouTube VTuber 直播（视频+音频）并保留元数据。

不改动 Radio 录制流程；这是 clipper 自己的独立下载入口。
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from clip.config import clip_config


@dataclass
class Chapter:
    start: float
    end: float
    title: str


@dataclass
class LiveMeta:
    url: str
    title: str
    description: str
    duration: float
    video_path: Path
    chapters: list[Chapter] = field(default_factory=list)


def probe_meta(url: str) -> dict:
    """不下载，仅取标题/上传日期/时长（用于决定归档目录名）。"""
    cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--no-warnings",
           "-O", "%(title)s\t%(upload_date)s\t%(duration)s", "--no-playlist", url]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 取元数据失败：\n{proc.stderr[-600:]}")
    title, upload_date, duration = (proc.stdout.strip().split("\t") + ["", "", ""])[:3]
    return {"title": title, "upload_date": upload_date, "duration": duration}


def safe_dirname(text: str, limit: int = 80) -> str:
    import re
    # 去掉 【】「」『』 等括号：radio_kg 的 parse_folder_metadata 会把首个【…】当作
    # episode_label，导致 label 坍缩成「歌枠」这类非唯一值。去括号后 label 回退为
    # 唯一的「日期_标题」。
    cleaned = re.sub(r"[【】「」『』〔〕〈〉《》\[\]]", " ", text)
    cleaned = re.sub(r'[/\\:*?"<>|\n\r\t]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return (cleaned or "live")[:limit]


def archive_episode_dir(recordings_root: Path, collection_id: str, meta: dict) -> Path:
    """归档方案：RADIO_DATA_DIR/<collection>/<YYYY-MM-DD>_<title>/。"""
    d = meta.get("upload_date") or ""
    date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else "unknown-date"
    label = safe_dirname(f"{date}_{meta.get('title','')}")
    return recordings_root / collection_id / label


def download_live(url: str, dest_dir: Path) -> LiveMeta:
    dest_dir.mkdir(parents=True, exist_ok=True)
    res = clip_config.clip_video_res
    out_tmpl = str(dest_dir / "source.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", f"bv*[height<=?{res}]+ba/b[height<=?{res}]",
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--no-playlist",
        "-o", out_tmpl,
        url,
    ]
    print(f"  $ yt-dlp -f bv*[height<=?{res}]+ba/b ... {url}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp 下载失败：\n{proc.stderr[-1000:]}")

    video = _find_one(dest_dir, (".mp4", ".mkv", ".webm"))
    info = _find_one(dest_dir, (".info.json",))
    meta = json.loads(info.read_text(encoding="utf-8")) if info else {}
    chapters = [
        Chapter(start=float(c.get("start_time", 0)),
                end=float(c.get("end_time", 0)),
                title=c.get("title", ""))
        for c in (meta.get("chapters") or [])
    ]
    return LiveMeta(
        url=url,
        title=meta.get("title", video.stem if video else ""),
        description=meta.get("description", "") or "",
        duration=float(meta.get("duration", 0) or 0),
        video_path=video,
        chapters=chapters,
    )


def _find_one(folder: Path, suffixes: tuple[str, ...]) -> Path | None:
    for p in sorted(folder.iterdir()):
        name = p.name.lower()
        if any(name.endswith(s) for s in suffixes):
            return p
    return None
