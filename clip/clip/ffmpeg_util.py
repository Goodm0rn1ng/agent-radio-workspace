"""ffmpeg/ffprobe 定位与小工具（本地实现，避免跨仓库路径注入）。"""
from __future__ import annotations

import json
import shutil
import subprocess


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as e:
        raise RuntimeError("找不到 ffmpeg；请安装 ffmpeg 或 imageio-ffmpeg。") from e


def ffprobe_bin() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("找不到 ffprobe；请安装 ffmpeg。")
    return exe


def run(cmd: list[str], cwd: str | None = None) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(cmd[:3])}…\n{proc.stderr[-800:]}")


def probe_duration(path: str) -> float:
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


def has_video_stream(path: str) -> bool:
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    return "video" in out.stdout
