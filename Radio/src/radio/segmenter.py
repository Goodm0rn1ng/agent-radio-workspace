"""音频切片：ffmpeg 把长音频按时长切成小段。

切点默认做**静音对齐**：在目标时长附近找最近的静音区间中点下刀，
避免固定时长硬切把句子拦腰截断（截断是「听不全」的主要来源——
被切开的半句在两个切片里都难以被 Whisper 正确转写）。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from loguru import logger

from radio.utils.ffmpeg import find_ffmpeg

# silencedetect 参数：低于 -35dB 持续 0.6s 以上视为静音（广播/直播谈话的自然停顿）
_SILENCE_NOISE = "-35dB"
_SILENCE_MIN_DUR = 0.6
# 在目标切点 ±(segment_seconds * 此比例) 窗口内找静音；找不到就硬切兜底
_ALIGN_WINDOW_RATIO = 0.4


async def _probe_silences(
    ffmpeg: str, input_path: Path
) -> tuple[list[tuple[float, float]], float]:
    """单遍解码，返回（静音区间列表, 音频总时长秒）。失败返回 ([], 0)。"""
    cmd = [
        ffmpeg,
        "-i",
        str(input_path),
        "-af",
        f"silencedetect=noise={_SILENCE_NOISE}:d={_SILENCE_MIN_DUR}",
        "-f",
        "null",
        "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    text = stderr.decode("utf-8", errors="replace")

    silences: list[tuple[float, float]] = []
    start: float | None = None
    for m in re.finditer(r"silence_(start|end):\s*([0-9.]+)", text):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif start is not None:
            silences.append((start, val))
            start = None

    duration = 0.0
    # 取最后一个 time=HH:MM:SS.xx（null muxer 实际处理到的末尾，比 Duration 头更可靠）
    times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if times:
        h, mnt, s = times[-1]
        duration = int(h) * 3600 + int(mnt) * 60 + float(s)
    return silences, duration


def _plan_cut_times(
    duration: float,
    segment_seconds: int,
    silences: list[tuple[float, float]],
) -> list[float]:
    """为每个目标切点（k*segment_seconds）选最近的静音中点；窗口内没有则硬切。"""
    if duration <= segment_seconds:
        return []
    window = segment_seconds * _ALIGN_WINDOW_RATIO
    midpoints = [(s + e) / 2 for s, e in silences]

    cuts: list[float] = []
    target = float(segment_seconds)
    while target < duration - 1.0:
        candidates = [m for m in midpoints if abs(m - target) <= window]
        # 切点必须严格递增，且与上一切点至少隔 segment_seconds 的一半
        floor = (cuts[-1] if cuts else 0.0) + segment_seconds * 0.5
        candidates = [m for m in candidates if m > floor]
        cut = min(candidates, key=lambda m: abs(m - target)) if candidates else max(target, floor)
        if cut < duration - 1.0:
            cuts.append(round(cut, 2))
        target = cut + segment_seconds
    return cuts


async def segment_audio(
    input_path: Path,
    output_dir: Path,
    segment_seconds: int = 600,
    silence_align: bool = True,
) -> list[tuple[Path, float]]:
    """把 input_path 切成约 segment_seconds 一段，返回 [(切片路径, 偏移秒), ...]。

    使用 ffmpeg 的 segment muxer，stream copy 不重编码，速度极快。
    silence_align=True 时切点对齐静音（误差 ±40% 段长），失败自动回退固定时长。
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

    cut_times: list[float] = []
    if silence_align:
        try:
            silences, duration = await _probe_silences(ffmpeg, input_path)
            if duration > 0:
                cut_times = _plan_cut_times(duration, segment_seconds, silences)
                aligned = sum(
                    1
                    for c in cut_times
                    if any(s <= c <= e for s, e in silences)
                )
                logger.info(
                    f"静音对齐切点：{len(cut_times)} 个（其中 {aligned} 个落在静音内，"
                    f"检出静音 {len(silences)} 段，时长 {duration:.0f}s）"
                )
        except Exception as e:
            logger.warning(f"silencedetect 失败，回退固定时长切片：{e!r}")
            cut_times = []

    pattern = output_dir / f"seg_%03d.{ext}"
    cmd = [ffmpeg, "-y", "-i", str(input_path), "-f", "segment"]
    if cut_times:
        cmd += ["-segment_times", ",".join(f"{t:.2f}" for t in cut_times)]
    else:
        cmd += ["-segment_time", str(segment_seconds)]
    cmd += ["-c", "copy", "-loglevel", "error", str(pattern)]

    logger.info(
        f"ffmpeg 切片：{input_path.name} → "
        + (f"{len(cut_times) + 1} 段（静音对齐）" if cut_times else f"每 {segment_seconds}s 一段")
    )
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

    if cut_times and len(paths) == len(cut_times) + 1:
        offsets = [0.0] + cut_times
        result = list(zip(paths, offsets))
    else:
        if cut_times:
            logger.warning(
                f"切片数({len(paths)})与切点数({len(cut_times)})不符，按固定时长回推偏移"
            )
        result = [(p, i * segment_seconds * 1.0) for i, p in enumerate(paths)]
    logger.info(f"切片完成：{len(result)} 段")
    return result
