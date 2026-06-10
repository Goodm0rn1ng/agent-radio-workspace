"""YouTube live recording source.

This mirrors the Radiko live source at the boundary used by the pipeline:
URL + duration in, audio file out. yt-dlp handles YouTube's live manifest
details; ffmpeg caps the recording length so scheduled jobs do not run forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from radio.live_detector import probe_youtube_live, wait_for_youtube_live
from radio.utils.ffmpeg import find_ffmpeg

# Grace period for yt-dlp to mux/extract audio after we ask it to stop.
_FINALIZE_TIMEOUT_S = 120


@dataclass(frozen=True)
class YouTubeLiveAudio:
    """Recorded YouTube live audio plus source metadata."""

    audio_path: Path
    title: str
    source_url: str


async def record_youtube_live(
    url: str,
    output_dir: Path,
    *,
    duration_minutes: int,
    title: str | None = None,
    cookies_path: Path | None = None,
    detection_timeout_minutes: int = 30,
    detection_interval_seconds: int = 60,
    wait_for_live: bool = True,
    audio_format: str = "m4a",
) -> YouTubeLiveAudio:
    """Record a YouTube live stream for a fixed duration and return audio."""
    if duration_minutes <= 0:
        raise ValueError("duration_minutes 必须大于 0")

    output_dir.mkdir(parents=True, exist_ok=True)
    if wait_for_live:
        live_info = await wait_for_youtube_live(
            url,
            timeout_minutes=detection_timeout_minutes,
            interval_seconds=detection_interval_seconds,
            cookies_path=cookies_path,
        )
    else:
        live_info = await asyncio.to_thread(
            probe_youtube_live,
            url,
            cookies_path=cookies_path,
        )

    display_title = title or live_info.title
    record_url = live_info.webpage_url or url
    before = {p.resolve() for p in output_dir.glob("*") if p.is_file()}

    logger.info(
        f"YouTube live recording start: duration={duration_minutes} min, url={record_url}"
    )
    await _run_ytdlp_record(
        record_url,
        output_dir,
        duration_minutes=duration_minutes,
        cookies_path=cookies_path,
        audio_format=audio_format,
    )
    audio_path = _find_downloaded_audio(output_dir, before, audio_format)
    size_mb = audio_path.stat().st_size / 1024 / 1024
    logger.info(f"YouTube live audio ready: {audio_path.name} ({size_mb:.1f} MB)")
    return YouTubeLiveAudio(
        audio_path=audio_path,
        title=display_title,
        source_url=record_url,
    )


async def _run_ytdlp_record(
    url: str,
    output_dir: Path,
    *,
    duration_minutes: int,
    cookies_path: Path | None,
    audio_format: str,
) -> None:
    ffmpeg = find_ffmpeg()
    duration_s = duration_minutes * 60
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-playlist",
        "--live-from-start",
        "--format",
        "bestaudio/best",
        "--downloader",
        "ffmpeg",
        "--downloader-args",
        f"ffmpeg_i:-t {duration_s}",
        "--ffmpeg-location",
        ffmpeg,
        "--extract-audio",
        "--audio-format",
        audio_format,
        "--audio-quality",
        "0",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--output",
        str(output_dir / "youtube_live.%(ext)s"),
    ]
    if cookies_path is not None:
        if not cookies_path.exists():
            raise FileNotFoundError(f"cookies 文件不存在：{cookies_path}")
        cmd.extend(["--cookies", str(cookies_path)])
    cmd.append(url)

    # --live-from-start forces yt-dlp's native dashsegments downloader, which
    # ignores the ffmpeg "-t" cap above, so recording would run unbounded. Bound
    # it on a wall clock instead: after duration_s, send one SIGINT so yt-dlp
    # stops the live download and still mux/extract the audio it has captured.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=duration_s)
    except asyncio.TimeoutError:
        timed_out = True
        logger.info(f"YouTube live recording reached {duration_minutes} min, stopping")
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_FINALIZE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            _kill_process_group(proc)
            stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # Job canceled: stop the recording and reap its children, do not leak.
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGINT)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.communicate(), timeout=_FINALIZE_TIMEOUT_S)
        _kill_process_group(proc)
        raise
    # When we stopped it ourselves, a non-zero exit (e.g. SIGINT 130) is expected;
    # success is decided by whether audio was produced (_find_downloaded_audio).
    if not timed_out and proc.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"yt-dlp YouTube live 录制失败 (exit {proc.returncode}):\n{detail}")


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Hard-kill the subprocess and any children it spawned (own session)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


def _find_downloaded_audio(
    output_dir: Path,
    before: set[Path],
    audio_format: str,
) -> Path:
    expected = output_dir / f"youtube_live.{audio_format}"
    if expected.exists():
        return expected

    new_files = [
        p
        for p in output_dir.glob("*")
        if p.is_file()
        and p.resolve() not in before
        and p.suffix.lower() not in {".part", ".ytdl", ".json"}
    ]
    preferred = [
        p for p in new_files if p.suffix.lower().lstrip(".") == audio_format.lower()
    ]
    candidates = preferred or new_files
    if not candidates:
        raise FileNotFoundError(f"未找到 YouTube live 录制音频：{output_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)
