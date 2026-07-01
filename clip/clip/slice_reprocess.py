"""短片级 song/talk 重判。

手动切片的边界由人指定，整场 setlist 的 song_start/song_end 经常会因为摘要粗粒度、
ASR 歌词误识别或多首歌共用小节而错位。这里只把整场摘要当作曲名提示，时间边界
一律基于切出短片后的二次精听 ASR 重新判断。
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from clip.aligner import Cue
from clip.lyrics import SongSpan


_START_MARKERS = (
    "聞いてください", "聴いてください", "歌っていき", "歌います", "歌わせて",
    "いきたいと思います", "レッツゴー", "let'sgo", "letsgo",
)
_END_MARKERS = ("でした", "ありがとう", "ありがと", "最高", "おかえり")
_CHAT_MARKERS = (
    "ありがとう", "ありがと", "コメント", "スパチャ", "ですね", "じゃあ", "次",
    "みんな", "皆さん", "思う", "思って", "本当に", "すごい", "最高", "見たい",
    "ガルパ", "音ゲー", "ゲーム", "カバー", "ください", "でした", "おかえり",
    "これ", "なんか", "多分", "なので", "けど", "っていう", "使", "買",
    "円", "商品", "おすすめ", "美容", "メイク", "スキンケア", "髪", "匂い",
    "におい", "香り", "シャンプー", "香水", "スプレー", "男の子", "女の子",
    "ドラッグストア", "イベント", "推し",
)
_GENERIC_SONG_TITLES = {"", "歌", "歌曲", "song", "songs"}
_POST_SONG_TALK_MARKERS = ("これは", "平成", "ガルパ", "音ゲー", "スパチャ")
_TITLE_ALIASES = {
    "thisgame": ("ディスゲーム", "ディス ゲーム", "thisgame"),
    "godknows": ("ゴッドノウズ", "ゴッド ノウズ", "ごっどのうず", "ごとうのうず", "godknows"),
    "unravel": ("アンラベル", "アンラヴェル", "unravel"),
}


def infer_song_spans_for_cut(
    anchor_cues: list[Cue] | None,
    source_segments: list[dict],
    clip_start: float,
    clip_end: float,
    *,
    episode_dir: str | Path | None = None,
    llm=None,
) -> list[SongSpan]:
    """返回绝对秒 SongSpan。时间只基于切片 ASR，摘要只提供曲名候选。"""
    duration = max(0.0, clip_end - clip_start)
    lines = _group_anchor_cues(anchor_cues or [])
    if not lines:
        lines = _source_lines(source_segments, clip_start, clip_end)
    if not lines:
        return []

    hints = _summary_hints(episode_dir, clip_start, clip_end)
    llm_spans = _infer_with_llm(lines, hints, duration, llm) if llm else []
    spans = _merge_rel_spans(llm_spans or _infer_with_heuristics(lines, hints, duration))
    out = [
        SongSpan(clip_start + s.start, clip_start + s.end, s.title, artist=s.artist)
        for s in spans
        if s.end - s.start >= 8.0
    ]
    if out:
        labels = ", ".join(f"{s.title}@{s.start - clip_start:.1f}-{s.end - clip_start:.1f}" for s in out)
        print(f"  slice song/talk reprocess: {labels}")
    else:
        print("  slice song/talk reprocess: no song span")
    return out


def _coerce_sec(value, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            parts = [float(x) for x in value.strip().split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            return parts[0] if parts else default
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s　\"'`、。，．・!！?？:：;；/／\\\-\[\]（）()【】「」『』《》〈〉]+", "", text)


def _group_anchor_cues(anchor_cues: list[Cue], *, gap: float = 0.8,
                       max_dur: float = 6.0, max_chars: int = 28) -> list[Cue]:
    words = sorted((c for c in anchor_cues if (c.ja or "").strip()), key=lambda c: c.start)
    lines: list[Cue] = []
    group: list[Cue] = []

    def flush() -> None:
        if not group:
            return
        text = "".join((c.ja or "").strip() for c in group).strip()
        if text:
            lines.append(Cue(group[0].start, max(group[-1].end, group[0].start + 0.4), text, ""))
        group.clear()

    for cue in words:
        if group:
            dur = cue.end - group[0].start
            chars = sum(len((c.ja or "").strip()) for c in group)
            if cue.start - group[-1].end > gap or dur > max_dur or chars >= max_chars:
                flush()
        group.append(cue)
    flush()
    return lines


def _source_lines(segments: list[dict], clip_start: float, clip_end: float) -> list[Cue]:
    out: list[Cue] = []
    for seg in segments:
        st = _coerce_sec(seg.get("start"))
        en = _coerce_sec(seg.get("end"), st)
        if en <= clip_start or st >= clip_end:
            continue
        text = (seg.get("ja") or seg.get("text") or "").strip()
        if text:
            out.append(Cue(max(0.0, st - clip_start), min(clip_end, en) - clip_start, text, ""))
    return out


def _parse_section_range(section: dict) -> tuple[float, float] | None:
    parts = re.split(r"\s*[-–—]\s*", str(section.get("time_range") or ""), maxsplit=1)
    if len(parts) != 2:
        return None
    st, en = _coerce_sec(parts[0]), _coerce_sec(parts[1])
    return (st, en) if en > st else None


def _parse_music(raw: object) -> tuple[str, str | None] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    m = re.search(r"^(.*?)[「『《〈]([^」』》〉]{1,80})[」』》〉]", text)
    if m:
        artist = m.group(1).strip(" \t-—:：/／") or None
        return m.group(2).strip(), artist
    return text.strip(" \t-—:：/／"), None


def _summary_hints(episode_dir: str | Path | None, clip_start: float, clip_end: float) -> list[SongSpan]:
    if not episode_dir:
        return []
    p = Path(episode_dir) / "05_summary.json"
    if not p.exists():
        return []
    try:
        summary = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    ranked: list[tuple[int, SongSpan]] = []
    for section in summary.get("sections", []) or []:
        rng = _parse_section_range(section)
        if not rng:
            continue
        sec_start, sec_end = rng
        if min(clip_end, sec_end) - max(clip_start, sec_start) <= 0:
            continue
        entries = [x for x in (_parse_music(raw) for raw in (section.get("music") or [])) if x]
        if not entries:
            continue
        progress = (max(clip_start, sec_start) - sec_start) / max(sec_end - sec_start, 1.0)
        likely = max(0, min(len(entries) - 1, int(progress * len(entries))))
        for i, (title, artist) in enumerate(entries):
            rank = 0 if i == likely else abs(i - likely) + 1
            ranked.append((rank, SongSpan(clip_start, clip_end, title, artist=artist)))
    out: list[SongSpan] = []
    seen: set[str] = set()
    for _, hint in sorted(ranked, key=lambda x: x[0]):
        key = _norm(hint.title)
        if key and key not in seen:
            seen.add(key)
            out.append(hint)
    return out


class _RelSong:
    def __init__(self, start: float, end: float, title: str, artist: str | None = None):
        self.start = start
        self.end = end
        self.title = title
        self.artist = artist


def _format_lines(lines: list[Cue], limit: int = 180) -> str:
    out = []
    for c in lines[:limit]:
        out.append(f"[{c.start:.2f}-{c.end:.2f}] {c.ja}")
    if len(lines) > limit:
        out.append("...（后续省略）")
    return "\n".join(out)


def _format_hints(hints: list[SongSpan]) -> str:
    return "\n".join(f"- {h.title}" + (f" / {h.artist}" if h.artist else "") for h in hints[:20])


_LLM_SYSTEM = """你是手动切片后的短片 song/talk 分段助手。输入是一段已经切出来的视频的二次精听 ASR，时间是相对短片开头的秒数。
任务：只判断这段短片内部哪些区间是实际唱歌(song)，哪些是说话(talk)。不要沿用整场直播旧时间轴；候选曲名只可作为认曲提示。

规则：
- song_start/song_end 必须来自短片 ASR 中已经出现的唱歌/歌词/伴奏开唱证据，不能覆盖尚未开始的未来歌曲。
- 报曲名、闲聊、唱后感想、评论互动、Superchat、游戏/企划谈话都是 talk。
- 如果先说“接下来唱 X / 聴いてください”，song_start 应落在随后真正开唱或连续歌词开始处；不要把前置 talk 全盖住。
- 如果唱完后出现“でした/ありがとう/这是某年歌/ガルパ/音游”等感想，song_end 应在这些 talk 之前或最多包含极短收尾。
- ASR 可能有同音错字、歌词错字、罗马字/日文混杂，要用上下文推理。不要输出歌词全文。

严格输出 JSON：
{"songs":[{"title":"曲名，不确定则用候选中最可能曲名或 歌曲","artist":"原唱/原作者，可空","start":相对开始秒,"end":相对结束秒,"confidence":0.0到1.0}]}"""


def _infer_with_llm(lines: list[Cue], hints: list[SongSpan], duration: float, llm) -> list[_RelSong]:
    payload = (
        f"短片长度：{duration:.2f} 秒\n"
        f"候选曲名（只作认曲提示，不能使用其旧时间）：\n{_format_hints(hints) or '（无）'}\n\n"
        f"二次精听 ASR：\n{_format_lines(lines)}"
    )
    try:
        data = llm.complete_json(_LLM_SYSTEM, payload, max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] slice song/talk LLM failed, using heuristics: {e}")
        return []
    songs = data.get("songs", []) if isinstance(data, dict) else []
    out: list[_RelSong] = []
    for item in songs:
        if not isinstance(item, dict):
            continue
        st = max(0.0, min(duration, _coerce_sec(item.get("start"))))
        en = max(0.0, min(duration, _coerce_sec(item.get("end"))))
        conf = _coerce_sec(item.get("confidence"), 0.0)
        if en - st < 8.0 or conf < 0.35:
            continue
        if _talk_density(lines, st, en) > 0.45:
            continue
        title = (item.get("title") or "").strip() or _pick_title("", hints)
        if not _has_song_evidence(lines, st, en, hints, title):
            continue
        artist = (item.get("artist") or "").strip() or _artist_for_title(title, hints)
        out.append(_RelSong(st, en, title or "歌曲", artist or None))
    return out


def _infer_with_heuristics(lines: list[Cue], hints: list[SongSpan], duration: float) -> list[_RelSong]:
    out: list[_RelSong] = []
    i = 0
    while i < len(lines):
        callout = _find_next_callout(lines, i, hints)
        if callout is None:
            if out:
                break
            run = _find_lyric_run(lines, i)
            if run is None:
                break
            start_idx, title = run, _pick_title("", hints)
        else:
            start_idx = _first_songish_after(lines, callout + 1, min_start=lines[callout].end)
            title = _pick_title_near(lines, callout, hints)
            if start_idx is None:
                i = callout + 1
                continue
        end_idx = _find_song_end(lines, start_idx)
        if end_idx is None:
            break
        st = lines[start_idx].start
        en = lines[end_idx].end
        if (en - st >= 8.0 and _talk_density(lines, st, en) <= 0.55
                and _has_song_evidence(lines, st, en, hints, title)):
            out.append(_RelSong(st, min(en, duration), title or "歌曲", _artist_for_title(title, hints)))
        i = max(end_idx + 1, start_idx + 1)
    return out


def _merge_rel_spans(spans: list[_RelSong], *, max_gap: float = 35.0) -> list[_RelSong]:
    if len(spans) < 2:
        return spans
    ordered = sorted(spans, key=lambda s: s.start)
    out = [ordered[0]]
    for span in ordered[1:]:
        prev = out[-1]
        same_title = _norm(prev.title) == _norm(span.title) or prev.title == "歌曲" or span.title == "歌曲"
        if same_title and span.start - prev.end <= max_gap:
            prev.end = max(prev.end, span.end)
            if prev.title == "歌曲" and span.title != "歌曲":
                prev.title = span.title
                prev.artist = span.artist
            elif not prev.artist:
                prev.artist = span.artist
            continue
        out.append(span)
    return out


def _find_next_callout(lines: list[Cue], start_idx: int, hints: list[SongSpan]) -> int | None:
    for i in range(start_idx, len(lines)):
        text = lines[i].ja or ""
        if any(m in text for m in _START_MARKERS) or _pick_title(text, hints, require_match=True):
            return i
    return None


def _first_songish_after(lines: list[Cue], start_idx: int, *, min_start: float) -> int | None:
    for i in range(start_idx, len(lines)):
        if lines[i].start - min_start > 45.0:
            return None
        if _is_songish(lines[i].ja):
            return i
    return None


def _find_lyric_run(lines: list[Cue], start_idx: int) -> int | None:
    streak = 0
    first = None
    prev_end = None
    for i in range(start_idx, len(lines)):
        gap = 0.0 if prev_end is None else lines[i].start - prev_end
        if gap > 18.0:
            streak = 0
            first = None
        if _is_songish(lines[i].ja):
            first = i if first is None else first
            streak += 1
            if streak >= 3:
                return first
        else:
            streak = 0
            first = None
        prev_end = lines[i].end
    return None


def _find_song_end(lines: list[Cue], start_idx: int) -> int | None:
    last_song = start_idx
    min_end = lines[start_idx].start + 45.0
    for i in range(start_idx, len(lines)):
        text = lines[i].ja or ""
        if _is_songish(text) or lines[i].start < min_end:
            last_song = i
        if lines[i].start >= min_end and any(m in text for m in _END_MARKERS):
            if "でした" not in text and _has_songish_soon(lines, i):
                continue
            return i if "でした" in text else max(start_idx, i - 1)
        if lines[i].start >= min_end and any(m in text for m in _POST_SONG_TALK_MARKERS):
            return last_song
        nxt_gap = (lines[i + 1].start - lines[i].end) if i + 1 < len(lines) else 999.0
        if lines[i].start >= min_end and nxt_gap > 35.0:
            return last_song
        if lines[i].start >= min_end and _is_chat(text) and i > last_song + 1:
            if _has_songish_soon(lines, i):
                continue
            return last_song
    return last_song if last_song >= start_idx else None


def _has_songish_soon(lines: list[Cue], idx: int, *, within: float = 35.0) -> bool:
    base = lines[idx].end
    for j in range(idx + 1, len(lines)):
        if lines[j].start - base > within:
            return False
        if _is_songish(lines[j].ja):
            return True
    return False


def _is_chat(text: str) -> bool:
    s = text or ""
    if len(_norm(s)) <= 2:
        return True
    return any(m in s for m in _CHAT_MARKERS)


def _is_songish(text: str) -> bool:
    s = text or ""
    n = _norm(s)
    if len(n) <= 2:
        return False
    if any(m in s for m in _START_MARKERS):
        return False
    if _is_chat(s):
        return False
    return _looks_like_lyric(s)


def _looks_like_lyric(text: str) -> bool:
    s = text or ""
    lyric_terms = ("君", "僕", "私", "夢", "世界", "心", "未来", "命", "信じ", "勝て", "weare", "nonono")
    n = _norm(s)
    return any(t in s for t in lyric_terms[:9]) or any(t in n for t in lyric_terms[9:])


def _talk_density(lines: list[Cue], start: float, end: float) -> float:
    inside = [c for c in lines if c.end > start and c.start < end]
    if not inside:
        return 0.0
    return sum(1 for c in inside if _is_chat(c.ja)) / len(inside)


def _has_song_evidence(
    lines: list[Cue],
    start: float,
    end: float,
    hints: list[SongSpan],
    title: str,
) -> bool:
    inside = [c for c in lines if c.end > start and c.start < end]
    if not inside:
        return False
    if any(any(m in (c.ja or "") for m in _START_MARKERS) for c in inside):
        return True
    if any(_pick_title(c.ja, hints, require_match=True) for c in inside):
        return True
    if _title_matches_hint(title, hints):
        return True
    streak = 0
    for c in inside:
        if _is_songish(c.ja):
            streak += 1
            if streak >= 4 and _talk_density(inside, start, end) <= 0.30:
                return True
        else:
            streak = 0
    return False


def _title_matches_hint(title: str, hints: list[SongSpan]) -> bool:
    key = _norm(title)
    if not key or key in {_norm(x) for x in _GENERIC_SONG_TITLES}:
        return False
    for hint in hints:
        other = _norm(hint.title)
        if other and (key == other or key in other or other in key):
            return True
    return False


def _pick_title(text: str, hints: list[SongSpan], *, require_match: bool = False) -> str:
    text_norm = _norm(text)
    for hint in hints:
        key = _norm(hint.title)
        aliases = _TITLE_ALIASES.get(key, ())
        if key and (key in text_norm or any(_norm(a) in text_norm for a in aliases)):
            return hint.title
    return "" if require_match else (hints[0].title if hints else "歌曲")


def _pick_title_near(lines: list[Cue], idx: int, hints: list[SongSpan]) -> str:
    for j in range(max(0, idx - 2), min(len(lines), idx + 5)):
        title = _pick_title(lines[j].ja, hints, require_match=True)
        if title:
            return title
    return _pick_title(lines[idx].ja, hints)


def _artist_for_title(title: str, hints: list[SongSpan]) -> str | None:
    key = _norm(title)
    for hint in hints:
        other = _norm(hint.title)
        if key and other and (key == other or key in other or other in key):
            return hint.artist
    return None
