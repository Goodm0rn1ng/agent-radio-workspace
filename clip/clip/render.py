"""可复用的「切一段 + 二次精听字幕 + 烧录」：被 CLI 渲染、Telegram 点击切片共用。

字幕来源：原期 04_bilingual 提供分句与中文译文；切出短片后再跑「二次精听」ASR
（Kotoba-Whisper / 可选 Parakeet-mlx + 强制对齐，见 aligner/whisperx_worker）：用更准的
重识别词级结果校正每条谈话日文、并丢弃 VAD 判为静音处的幻觉句。歌唱区间的正文按 lyrics.py
的三档策略（占位 / 已授权歌词文件 / 授权网易云）处理，不复刻歌词。

纯歌唱片段（无谈话可校正）跳过二次精听以省时。
"""
from __future__ import annotations

import json
from pathlib import Path

from clip.aligner import Cue, _whisperx_cues, _write_srt
from clip.ffmpeg_util import ffmpeg_bin, has_video_stream, run
from clip.lyrics import SongSpan, _overlaps, anchor_cues_are_usable, build_clip_cues
from clip.packager import package


def _load_bilingual(episode_dir: Path, profile=None) -> list[dict]:
    p = Path(episode_dir) / "04_bilingual_segments.json"
    if not p.exists():
        return []
    segs = json.loads(p.read_text(encoding="utf-8"))
    if profile is not None:
        fix = dict(getattr(profile, "name_corrections", {}) or {})
        fix.update(getattr(profile, "terminology", {}) or {})
        for s in segs:
            for k, v in fix.items():
                s["ja"] = (s.get("ja") or "").replace(k, v)
                s["zh"] = (s.get("zh") or "").replace(k, v)
    return segs


def _terminology_str(profile) -> str:
    """把节目方案的术语/名字纠正整理成「原文 => 译法」清单，注入翻译 prompt 的术语库。"""
    if profile is None:
        return ""
    terms = dict(getattr(profile, "name_corrections", {}) or {})
    terms.update(getattr(profile, "terminology", {}) or {})
    return "\n".join(f"{k} => {v}" for k, v in terms.items() if k and v)


def _translation_prompt_path(profile):
    return getattr(profile, "translation_prompt_path", None) if profile is not None else None


def prepare_segment(video: str | Path, start: float, end: float, out_dir: Path, idx: int,
                    *, episode_dir: str | Path | None = None, profile=None,
                    song_spans: list[SongSpan] | None = None,
                    pad: float = 0.0,
                    llm_provider: str | None = None,
                    llm_model: str | None = None,
                    force_llm_retranslate: bool = False,
                    slice_reprocess_song_spans: bool = False) -> tuple[Path, list[Cue]]:
    """切 [start-pad, end+pad] → 生成中日字幕 cue（含译文修复），返回 (切片mp4, cues)。不烧录。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    video = str(video)
    s0 = max(0.0, start - pad)
    dur = (end + pad) - s0
    cut = out_dir / f"clip_{idx:02d}.mp4"
    is_video = has_video_stream(video)
    cmd = [ffmpeg_bin(), "-y", "-ss", f"{s0:.3f}", "-i", video, "-t", f"{dur:.3f}"]
    cmd += (["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"] if is_video
            else ["-vn", "-c:a", "aac"])
    cmd.append(str(cut))
    run(cmd)

    segs = _load_bilingual(episode_dir, profile) if episode_dir else []
    spans = song_spans or []
    has_talk = slice_reprocess_song_spans or any(
        not (en <= s0 or st >= end + pad) and not any(_overlaps(st, en, sp.start, sp.end) for sp in spans)
        for st, en in ((float(s.get("start", 0)), float(s.get("end", 0))) for s in segs)
    )
    anchor_cues = _whisperx_cues(cut) if has_talk else None   # 纯歌唱片段无需二次精听，省时
    if anchor_cues and anchor_cues_are_usable(anchor_cues, dur):
        print(f"  二次精听(Parakeet/Kotoba) -> {len(anchor_cues)} 词")
    elif anchor_cues:
        print(f"  精细时间轴过稀疏({len(anchor_cues)} 句/{dur:.0f}s)，沿用全程字幕时间轴")
    else:
        print("  精细时间轴不可用，沿用全程字幕时间轴")
    llm = None
    if has_talk or llm_provider or llm_model:      # 纯歌唱段只在用户选定 LLM 时初始化，供曲名检索复用
        try:
            from src.llm.client import LLMClient
            llm = LLMClient(provider=llm_provider, model=llm_model)
        except Exception as e:  # noqa: BLE001
            if llm_provider or llm_model:
                raise RuntimeError(f"所选 LLM 初始化失败：{e}") from e
            # 默认模式无 LLM 时退回「复用原译文」，不阻断切片。
            llm = None
    if slice_reprocess_song_spans:
        from clip.slice_reprocess import infer_song_spans_for_cut
        spans = infer_song_spans_for_cut(
            anchor_cues,
            segs,
            s0,
            end + pad,
            episode_dir=episode_dir,
            llm=llm,
        )
    cues = build_clip_cues(
        segs,
        s0,
        end + pad,
        spans,
        anchor_cues=anchor_cues,
        llm=llm,
        terminology=_terminology_str(profile),
        translation_prompt_path=_translation_prompt_path(profile),
        force_llm_retranslate=force_llm_retranslate,
    )
    return cut, cues


def assemble_segment(cut: Path, cues: list[Cue], out_dir: Path, idx: int,
                     *, accent=(255, 255, 255)) -> Path:
    """把（可能经人工审核/修改的）cues 烧录到切片 → 返回成片 mp4。"""
    srt = out_dir / f"clip_{idx:02d}.srt"
    _write_srt(cues, srt)
    return package(cut, srt, out_dir, idx, accent=accent)


def render_segment(video: str | Path, start: float, end: float, out_dir: Path, idx: int,
                   *, episode_dir: str | Path | None = None, profile=None,
                   song_spans: list[SongSpan] | None = None, pad: float = 0.0,
                   llm_provider: str | None = None,
                   llm_model: str | None = None,
                   force_llm_retranslate: bool = False,
                   slice_reprocess_song_spans: bool = False) -> Path:
    """一气通贯：切片 → 字幕 → 烧录 → 返回成片 mp4。（Telegram/CLI 路径用，行为不变）"""
    cut, cues = prepare_segment(video, start, end, out_dir, idx,
                                episode_dir=episode_dir, profile=profile,
                                song_spans=song_spans, pad=pad,
                                llm_provider=llm_provider,
                                llm_model=llm_model,
                                force_llm_retranslate=force_llm_retranslate,
                                slice_reprocess_song_spans=slice_reprocess_song_spans)
    accent = profile.accent_rgb((255, 255, 255)) if profile else (255, 255, 255)
    return assemble_segment(cut, cues, out_dir, idx, accent=accent)
