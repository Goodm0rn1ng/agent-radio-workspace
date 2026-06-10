"""Resolve an ffmpeg executable usable in bundled environments."""

from __future__ import annotations

import shutil


def find_ffmpeg() -> str:
    """Return a usable ffmpeg executable path."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None:
        return ffmpeg

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as e:
        raise RuntimeError(
            "ffmpeg 不在 PATH 中，且 imageio-ffmpeg 未安装。请 `uv sync` 或安装 ffmpeg。"
        ) from e
