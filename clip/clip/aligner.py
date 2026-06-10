"""字幕生成：WhisperX 词级 forced alignment（主路径），失败回退到已有逐句转写。

- 主路径：对切出的短 clip 跑 WhisperX（faster-whisper 转写 + wav2vec2 对齐）→ 词级时间戳。
  对两条分支通用（Branch B 无现成转写时也能用）。
- 回退：Branch A 直接用该期 04_bilingual_segments.json 在时间窗内的逐句文本（句级）。
最终统一产出双语 .srt（日文行 + 中文行）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from clip.config import clip_config
from clip.media_locator import _label_to_folder
from clip.models import MatchedClip
from src.llm.client import LLMClient


@dataclass
class Cue:
    start: float          # 相对 clip 起点的秒数
    end: float
    ja: str
    zh: str = ""


def _fmt_ts(sec: float) -> str:
    sec = max(sec, 0.0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def _parse_ts(ts: str) -> float:
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def parse_srt(path: Path) -> list[Cue]:
    """读回 .srt → Cue 列表（第一文本行=ja，第二行=zh）。供 packager 烧录使用。"""
    cues: list[Cue] = []
    for block in path.read_text(encoding="utf-8").strip().split("\n\n"):
        rows = [r for r in block.splitlines() if r.strip()]
        if len(rows) < 2 or "-->" not in rows[1]:
            continue
        start_s, end_s = (x.strip() for x in rows[1].split("-->"))
        text = rows[2:]
        cues.append(Cue(
            start=_parse_ts(start_s), end=_parse_ts(end_s),
            ja=text[0] if text else "",
            zh=text[1] if len(text) > 1 else "",
        ))
    return cues


def normalize_cues_for_display(
    cues: list[Cue],
    *,
    min_duration: float = 0.55,
    gap: float = 0.02,
) -> list[Cue]:
    """把字幕 cue 变成单轨显示时间轴，避免 hardcode 时多张字幕同时 overlay。

    网易云歌词里偶尔会有合唱/和声/逐词行，行级时间戳天然重叠；本项目只有一层
    hardcode 字幕，所以将相互重叠的一组 cue 均匀摊回该组的总时间窗。
    """
    cleaned = []
    for c in cues:
        if not (c.ja.strip() or c.zh.strip()):
            continue
        start = max(0.0, c.start)
        cleaned.append(Cue(start, max(c.end, start + 0.1), c.ja.strip(), c.zh.strip()))
    cleaned.sort(key=lambda c: (c.start, c.end))

    deduped: list[Cue] = []
    for c in cleaned:
        if deduped and _cue_text_key(deduped[-1]) == _cue_text_key(c) and deduped[-1].end > c.start:
            deduped[-1].start = min(deduped[-1].start, c.start)
            deduped[-1].end = max(deduped[-1].end, c.end)
            continue
        deduped.append(c)

    out: list[Cue] = []
    i = 0
    while i < len(deduped):
        group = [deduped[i]]
        group_end = deduped[i].end
        i += 1
        while i < len(deduped) and deduped[i].start < group_end - 0.001:
            group.append(deduped[i])
            group_end = max(group_end, deduped[i].end)
            i += 1
        if len(group) == 1:
            c = group[0]
            out.append(Cue(c.start, max(c.end, c.start + min_duration), c.ja, c.zh))
        else:
            out.extend(_spread_overlapping_group(group, min_duration=min_duration, gap=gap))

    # Final guard: never emit overlapping adjacent cues, even after rounding/tight spans.
    for prev, cur in zip(out, out[1:]):
        if prev.end > cur.start - gap:
            prev.end = max(prev.start + 0.1, cur.start - gap)
    return [c for c in out if c.end > c.start]


def _cue_text_key(c: Cue) -> str:
    return "".join((c.ja + "\n" + c.zh).split())


def _spread_overlapping_group(
    group: list[Cue],
    *,
    min_duration: float,
    gap: float,
) -> list[Cue]:
    start = min(c.start for c in group)
    end = max(c.end for c in group)
    available = max(0.1, (end - start) - gap * (len(group) - 1))
    original = [max(0.1, c.end - c.start) for c in group]

    if available < min_duration * len(group):
        durations = [available / len(group)] * len(group)
    else:
        total = sum(original) or 1.0
        durations = [max(min_duration, available * d / total) for d in original]
        overflow = sum(durations) - available
        while overflow > 0.0001:
            adjustable = [idx for idx, d in enumerate(durations) if d > min_duration + 0.0001]
            if not adjustable:
                break
            share = overflow / len(adjustable)
            for idx in adjustable:
                delta = min(share, durations[idx] - min_duration)
                durations[idx] -= delta
                overflow -= delta

    out: list[Cue] = []
    cur = start
    for c, dur in zip(group, durations):
        cue_end = cur + max(0.1, dur)
        out.append(Cue(cur, cue_end, c.ja, c.zh))
        cur = cue_end + gap
    return out


def _write_srt(cues: list[Cue], path: Path) -> None:
    cues = normalize_cues_for_display(cues)
    lines = []
    for i, c in enumerate(cues, 1):
        ja = _srt_text_line(c.ja)
        zh = _srt_text_line(c.zh)
        text = ja + (f"\n{zh}" if zh else "")
        lines.append(f"{i}\n{_fmt_ts(c.start)} --> {_fmt_ts(c.end)}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


def _srt_text_line(text: str) -> str:
    return " ".join(str(text or "").splitlines()).strip()


def _whisperx_cues(audio_path: Path) -> list[Cue] | None:
    """调用独立 venv 里的 WhisperX worker 做词级对齐 → 逐句 Cue（时间相对 clip 起点）。
    独立 venv 不存在或调用失败时返回 None 触发回退。"""
    import json
    import os
    import subprocess
    import tempfile

    py = clip_config.venv_python()
    if not py.exists():
        return None
    out_json = Path(tempfile.mktemp(suffix=".json"))
    cmd = [
        str(py), "-m", "clip.whisperx_worker", str(audio_path),
        clip_config.whisperx_language, clip_config.whisperx_model,
        clip_config.whisperx_device, clip_config.whisperx_compute_type, str(out_json),
        str(clip_config.whisperx_no_speech_max), str(clip_config.whisperx_logprob_min),
    ]
    # 清掉父 venv 的环境变量，避免 macOS 下子进程错误解析到主 venv 的 site-packages。
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")}
    if clip_config.parakeet_model:                 # 非空则 worker 优先尝试 Parakeet-mlx
        env["CLIP_PARAKEET_MODEL"] = clip_config.parakeet_model
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              cwd=str(clip_config.abspath(".")),
                              timeout=clip_config.whisperx_timeout_sec)
    except subprocess.TimeoutExpired:
        print("  [warn] WhisperX 对齐超时，回退到逐句转写")
        out_json.unlink(missing_ok=True)
        return None
    if proc.returncode != 0:
        print(f"  [warn] WhisperX 对齐失败，回退到逐句转写：{proc.stderr[-300:]}")
        out_json.unlink(missing_ok=True)
        return None
    try:
        segs = json.loads(out_json.read_text(encoding="utf-8"))
    finally:
        out_json.unlink(missing_ok=True)
    cues = [Cue(start=float(s["start"]), end=float(s["end"]), ja=s["text"]) for s in segs if s["text"]]
    return cues or None


def _json_cues(clip: MatchedClip) -> list[Cue]:
    """回退：从该期 04_bilingual_segments.json 取时间窗内逐句（已有 ja+zh），
    时间偏移到 clip 起点（含 pad）。"""
    folder = _label_to_folder().get(clip.episode_label)
    if folder is None:
        return []
    for fname in ("04_bilingual_segments.json", "03_ja_segments.json"):
        p = folder / fname
        if p.exists():
            segs = json.loads(p.read_text(encoding="utf-8"))
            break
    else:
        return []
    origin = max(0.0, clip.start - clip_config.clip_pad_sec)
    cues = []
    for s in segs:
        st, en = float(s.get("start", 0)), float(s.get("end", 0))
        if en < clip.start - 0.5 or st > clip.end + 0.5:
            continue
        cues.append(Cue(
            start=st - origin, end=en - origin,
            ja=(s.get("ja") or "").strip(),
            zh=(s.get("zh") or "").strip(),
        ))
    return cues


def _translate_missing(cues: list[Cue], llm: LLMClient) -> None:
    """给没有 zh 的 cue 批量补中文（WhisperX 路径只有日文）。"""
    todo = [c for c in cues if c.ja and not c.zh]
    if not todo:
        return
    numbered = "\n".join(f"{i+1}. {c.ja}" for i, c in enumerate(todo))
    try:
        data = llm.complete_json(
            "把下列日文逐行翻译成自然的简体中文。严格输出 JSON: "
            '{"lines": ["第1行中文", ...]}，顺序与数量一致。',
            numbered, max_tokens=2048,
        )
        zh = data.get("lines", [])
        for c, z in zip(todo, zh):
            c.zh = (z or "").strip()
    except Exception as e:  # noqa: BLE001 — 翻译失败就只留日文
        print(f"  [warn] 字幕翻译失败，仅保留日文：{e}")


_TRANSLATE_TEMPLATE_CACHE: dict[str, str | None] = {}


def _load_translate_template(path: str | Path | None = None) -> str | None:
    """Load the program prompt, falling back to Radio's default translate prompt."""
    p = Path(path) if path else (
        Path(__file__).resolve().parents[2]
        / "Radio" / "src" / "radio" / "prompts" / "translate.txt"
    )
    key = str(p)
    if key not in _TRANSLATE_TEMPLATE_CACHE:
        try:
            _TRANSLATE_TEMPLATE_CACHE[key] = p.read_text(encoding="utf-8") if p.exists() else None
        except Exception:  # noqa: BLE001
            _TRANSLATE_TEMPLATE_CACHE[key] = None
    return _TRANSLATE_TEMPLATE_CACHE[key]


def translate_lines(
    lines: list[str],
    llm: LLMClient | None = None,
    terminology: str = "",
    template_path: str | Path | None = None,
    prior_zh: list[str] | None = None,
) -> list[str]:
    """日文逐行 → 中文，优先用 Radio 精翻 prompt（术语库可注入）。返回与输入等长的中文列表。

    空行原样保留；翻译失败时该行返回空串（调用方决定是否保留原值）。
    """
    out = ["" for _ in lines]
    idxs = [i for i, s in enumerate(lines) if s and s.strip()]
    if not idxs:
        return out
    llm = llm or LLMClient()
    template = _load_translate_template(template_path)
    try:
        if template:
            payload = json.dumps(
                [
                    {
                        "i": i,
                        "ja": lines[i],
                        **(
                            {"prev_zh": prior_zh[i]}
                            if prior_zh and i < len(prior_zh) and prior_zh[i]
                            else {}
                        ),
                    }
                    for i in idxs
                ],
                ensure_ascii=False,
            )
            system = (template.replace("{terminology}", terminology or "（无额外术语）")
                              .replace("{input_json}", payload))
            data = llm.complete_json(
                system,
                "请先整体阅读 input_json 中的全部句子，理解上下文、指代、倒装和 ASR 可能错字；"
                "prev_zh 只是旧译参考，必须以 ja 为准重新逐句翻译。请严格按上述格式只输出 JSON。",
                max_tokens=8192,
            )
            segs = data.get("segments", []) if isinstance(data, dict) else []
            by_i = {int(s["i"]): (s.get("zh") or "").strip()
                    for s in segs if isinstance(s, dict) and "i" in s and "zh" in s}
            for i in idxs:
                out[i] = by_i.get(i, "")
        else:
            numbered = "\n".join(
                f"{k+1}. {lines[i]}"
                + (
                    f"\n   旧译参考：{prior_zh[i]}"
                    if prior_zh and i < len(prior_zh) and prior_zh[i]
                    else ""
                )
                for k, i in enumerate(idxs)
            )
            data = llm.complete_json(
                "先整体阅读下列短片字幕，理解上下文、指代、省略主语、倒装和 ASR 同音错字；"
                "再把日文逐行翻译成自然的简体中文。旧译参考只能辅助理解，必须以日文为准。"
                '严格输出 JSON: {"lines": ["第1行中文", ...]}，顺序与数量一致。',
                numbered, max_tokens=8192,
            )
            zh = data.get("lines", []) if isinstance(data, dict) else []
            for k, i in enumerate(idxs):
                out[i] = (zh[k] or "").strip() if k < len(zh) else ""
    except Exception as e:  # noqa: BLE001 — 翻译失败：返回空串，调用方保留原值
        print(f"  [warn] 逐行翻译失败：{e}")
    return out


def make_subtitles(clip: MatchedClip, cut_path: Path, out_dir: Path, idx: int,
                   llm: LLMClient | None = None) -> Path:
    cues = _whisperx_cues(cut_path)
    used = "whisperx(词级对齐)"
    if cues is None:
        cues = _json_cues(clip)
        used = "已有逐句转写(回退)"
    if not cues:
        raise RuntimeError("无可用字幕来源（WhisperX 未装且无现成转写）")
    from clip.lyrics import merge_asr_cues_with_authorized_lyrics
    cues, lyric_used = merge_asr_cues_with_authorized_lyrics(
        cues, [clip.matched_signal, clip.title, clip.text, clip.trend_topic]
    )
    if lyric_used:
        used = f"{used}+{lyric_used}"
    _translate_missing(cues, llm or LLMClient())
    srt = out_dir / f"clip_{idx:02d}.srt"
    _write_srt(cues, srt)
    print(f"  字幕({used}) -> {srt.name}  共 {len(cues)} 句")
    return srt
