"""切片「二次精听」worker —— 在**独立 venv**（.venv_whisperx）里运行。

ASR：优先 Parakeet-mlx（若配置了 parakeet-mlx 可加载的 JA 模型，Apple Silicon 原生，自带词级
时间戳）；否则用 Kotoba-Whisper（kotoba-whisper-v2.0-faster，JA 专用、faster-whisper 兼容）
+ WhisperX wav2vec2-ja 强制对齐。

去幻觉：WhisperX 自带 VAD（静音处不臆造）；再按 no_speech_prob / avg_logprob 丢弃疑似幻觉段，
合并连续重复段。输出**词级** [{start,end,text}]（相对 audio 起点）——上层据此校正既有字幕并丢弃
VAD 判定为静音处的幻觉句。

用法：
  <venv>/bin/python -m clip.whisperx_worker \
      <audio> <lang> <model> <device> <compute_type> <out_json> [no_speech_max] [logprob_min]
  环境变量 CLIP_PARAKEET_MODEL 非空时优先尝试 Parakeet-mlx。
"""
from __future__ import annotations

import json
import os
import re
import sys

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # HF Xet 后端本机会卡死，走经典下载


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "").strip()


def _try_parakeet(audio: str) -> list[dict] | None:
    """Parakeet-mlx：若配置了可加载的 JA 模型则用之（自带词级时间戳），否则 None 回退。"""
    model_id = os.environ.get("CLIP_PARAKEET_MODEL", "").strip()
    if not model_id:
        return None
    try:
        from parakeet_mlx import from_pretrained
        model = from_pretrained(model_id)
        res = model.transcribe(audio)
        out: list[dict] = []
        for sent in (getattr(res, "sentences", None) or []):
            toks = getattr(sent, "tokens", None) or []
            for tok in toks:
                txt = (getattr(tok, "text", "") or "").replace("▁", "").strip()  # 去 sentencepiece ▁
                if txt:
                    out.append({"start": float(tok.start), "end": float(tok.end), "text": txt})
            if not toks:
                txt = (getattr(sent, "text", "") or "").strip()
                if txt:
                    out.append({"start": float(sent.start), "end": float(sent.end), "text": txt})
        return out or None
    except Exception as e:  # noqa: BLE001 — Parakeet 不可用即回退 Kotoba
        print(f"[parakeet] 不可用，回退 Kotoba：{e}", file=sys.stderr)
        return None


def _kotoba_words(audio: str, lang: str, model_name: str, device: str,
                  compute_type: str, no_speech_max: float, logprob_min: float) -> list[dict]:
    import whisperx
    model = whisperx.load_model(model_name, device, compute_type=compute_type, language=lang)
    audio_data = whisperx.load_audio(audio)
    result = model.transcribe(audio_data, language=lang)

    # 去幻觉：丢弃高 no_speech / 低置信段 + 合并连续重复段（VAD 已先过滤静音）
    kept, prev = [], None
    for s in result.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if float(s.get("no_speech_prob", 0.0)) >= no_speech_max:
            continue
        if float(s.get("avg_logprob", 0.0)) <= logprob_min:
            continue
        n = _norm(text)
        if n and n == prev:
            continue
        prev = n
        kept.append(s)

    align_model, meta = whisperx.load_align_model(language_code=lang, device=device)
    aligned = whisperx.align(kept, align_model, meta, audio_data, device)
    words: list[dict] = []
    for seg in aligned.get("segments", []):
        ws = [w for w in (seg.get("words") or [])
              if w.get("start") is not None and w.get("end") is not None and (w.get("word") or "").strip()]
        if ws:
            for w in ws:
                words.append({"start": float(w["start"]), "end": float(w["end"]),
                              "text": (w["word"] or "").strip()})
        elif (seg.get("text") or "").strip():
            words.append({"start": float(seg["start"]), "end": float(seg["end"]),
                          "text": (seg["text"] or "").strip()})
    return words


def main() -> None:
    a = sys.argv[1:]
    audio, lang, model_name, device, compute_type, out_json = a[:6]
    no_speech_max = float(a[6]) if len(a) > 6 else 0.6
    logprob_min = float(a[7]) if len(a) > 7 else -1.1

    words = _try_parakeet(audio)
    if words is None:
        words = _kotoba_words(audio, lang, model_name, device, compute_type,
                              no_speech_max, logprob_min)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
