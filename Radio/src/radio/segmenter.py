"""音频切片：ffmpeg 把长音频按时长切成小段。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from loguru import logger

from radio.utils.ffmpeg import find_ffmpeg


async def segment_audio(
    input_path: Path,
    output_dir: Path,
    segment_seconds: int = 600,
) -> list[tuple[Path, float]]:
    """把 input_path 切成 segment_seconds 一段，返回 [(切片路径, 偏移秒), ...]。

    使用 ffmpeg 的 segment muxer，stream copy 不重编码，速度极快。
    """
    ffmpeg = find_ffmpeg()

    output_dir.mkdir(parents=True, exist_ok=True)
    # 输出扩展名沿用输入，避免 stream copy 容器不兼容。
    # m4a / mp4 都映射到 m4a；其他保留原扩展名（mp3/webm 等）。
    ext = input_path.suffix.lower().lstrip(".") or "m4a"
    if ext == "mp4":
        ext = "m4a"

    # 清掉旧切片
    for p in output_dir.glob(f"seg_*.{ext}"):
        p.unlink()

    pattern = output_dir / f"seg_%03d.{ext}"
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-c",
        "copy",
        "-loglevel",
        "error",
        str(pattern),
    ]
    logger.info(f"ffmpeg 切片：{input_path.name} → 每 {segment_seconds}s 一段")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 切片失败：{stderr.decode('utf-8', errors='replace')}"
        )

    paths = sorted(output_dir.glob(f"seg_*.{ext}"))
    if not paths:
        # 兜底：直接复制原文件作为单段（极短音频）
        single = output_dir / f"seg_000.{ext}"
        shutil.copy(input_path, single)
        paths = [single]

    result = [(p, i * segment_seconds * 1.0) for i, p in enumerate(paths)]
    logger.info(f"切片完成：{len(result)} 段")
    return result
