"""按时间戳用 ffmpeg 切片音频/视频。"""
from __future__ import annotations

from pathlib import Path

from clip.config import clip_config
from clip.ffmpeg_util import ffmpeg_bin, has_video_stream, run
from clip.models import MatchedClip


def slice_clip(clip: MatchedClip, out_dir: Path, idx: int) -> Path:
    """切出 [start-pad, end+pad] 区间。视频源出 mp4（重编码便于后续烧字幕），
    音频源出 m4a。返回切片路径。"""
    src = clip.media_path
    if not src or not Path(src).exists():
        raise FileNotFoundError(f"源媒体不存在：{src}")

    pad = clip_config.clip_pad_sec
    start = max(0.0, clip.start - pad)
    dur = (clip.end + pad) - start
    if dur <= 0:
        raise ValueError(f"片段时长非法：start={clip.start} end={clip.end}")

    is_video = has_video_stream(src)
    suffix = ".mp4" if is_video else ".m4a"
    out = out_dir / f"clip_{idx:02d}{suffix}"

    cmd = [ffmpeg_bin(), "-y", "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}"]
    if is_video:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
    else:
        cmd += ["-vn", "-c:a", "aac"]
    cmd.append(str(out))
    run(cmd)
    return out
