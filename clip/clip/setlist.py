"""从结构化摘要和全程时间轴里提取本场歌枠的「全曲清单」。

只提取**曲名**（标题不含歌词）和两类区间：
- clip_start/clip_end：适合切片的完整段落，可包含同一首歌的前置介绍/后置感想。
- song_start/song_end：实际演唱正文，用于歌词替换和对齐。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from src.llm.client import LLMClient

_SYSTEM = """你是 VTuber 歌枠的选曲与切片时间轴整理助手。给你一场直播的结构化摘要，以及可选的全程 ASR 时间轴。
请列出本场**实际完整演唱过的歌曲**。不要输出「可愛いアニソンブロック」「ラストソング」这类分区名；曲名请用日文原名优先。
artist 请尽量给出原唱/演唱者/角色组合名；曲名容易重名或标题很短时，artist 是必填级别的信息，用于后续检索授权歌词。

每首歌需要区分两类时间：
1. song_start/song_end：实际唱歌正文的开始/结束秒。依据连续歌词、伴奏进入/结束、演唱停顿判断。
2. clip_start/clip_end：适合切片发布的开始/结束秒。可以包含同一首歌紧邻的前置介绍、报曲名、作品说明、唱完后的短感想/道歉/互动；不要跨到上一首或下一首。若没有明确介绍/感想，就等于 song_start/song_end。

规则：
- 只列实际唱过的歌；不要列没唱的、BGM、片头等待、纯聊天。
- 不要输出任何歌词内容。
- 保证 clip_start <= song_start < song_end <= clip_end。
- 如果只能从摘要知道大致区间，也要给出保守估计并降低 confidence。

输出严格 JSON：
{"songs":[{"title":"曲名","artist":"原唱/歌手 或 空","origin":"原作/动画 或 空","clip_start":开始秒,"clip_end":结束秒,"song_start":演唱开始秒,"song_end":演唱结束秒,"confidence":0.0到1.0}]}"""

_REFINE_SYSTEM = """你是 VTuber 歌枠单曲切片时间轴校对助手。给你一个候选曲目、摘要上下文，以及该候选附近的 ASR 时间轴。
请只校对这一首候选对应的实际演唱歌曲：
- 如果候选 title 是作品名、分区名、乐队/话题名、粗略描述或容易误命中的裸标题，请结合摘要与附近 ASR 推断实际歌曲名（日文原名优先）。
- 请尽量给出原唱/歌手/角色组合 artist；当曲名容易重名、标题很短，或摘要只给了动画/话题时，artist 很重要。
- 可以根据附近 ASR 的歌词短句和摘要上下文识别歌曲，但不要把 ASR 误识别的歌词原文当成 title。
- song_start/song_end 是实际唱歌正文。
- clip_start/clip_end 可包含紧邻的报曲名、作品介绍、唱完短感想/道歉/互动，但不要包含上一首/下一首。
- 不要输出任何歌词内容。

输出严格 JSON：
{"song":{"title":"曲名","artist":"原唱/歌手 或 空","origin":"原作/动画 或 空","clip_start":开始秒,"clip_end":结束秒,"song_start":演唱开始秒,"song_end":演唱结束秒,"confidence":0.0到1.0}}"""


@dataclass
class Song:
    title: str
    origin: str
    start: float                  # 切片开始（兼容旧调用）
    end: float                    # 切片结束（兼容旧调用）
    song_start: float | None = None
    song_end: float | None = None
    clip_start: float | None = None
    clip_end: float | None = None
    artist: str | None = None
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "origin": self.origin,
            "artist": self.artist,
            "start": self.start,
            "end": self.end,
            "clip_start": self.clip_start if self.clip_start is not None else self.start,
            "clip_end": self.clip_end if self.clip_end is not None else self.end,
            "song_start": self.song_start if self.song_start is not None else self.start,
            "song_end": self.song_end if self.song_end is not None else self.end,
            "confidence": self.confidence,
        }


def _ts_to_sec(t: str) -> float:
    parts = [float(x) for x in str(t).split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return float(parts[0]) if parts else 0.0


def _coerce_sec(value, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            return _ts_to_sec(value.strip()) if value.strip() else default
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


_SING_HINTS = ("歌", "ソング", "song", "曲", "メドレー", "アニソン", "カバー",
               "ブロック", "歌枠", "うた", "ラスト")
_BRACKET_TITLE = re.compile(r"[「『《〈]([^」』》〉]{1,40})[」』》〉]")
_NON_SINGING = ("トーク", "talk", "オープニング", "エンディング", "雑談", "挨拶", "宣言")
_CHAT_MARKERS = (
    "ありがとう", "ありがとうございます", "コメント", "来年", "今年",
    "自分で", "思いました", "配信", "嬉しい", "お疲れ", "よろしく", "ござい", "ました",
    "でした", "じゃあ", "次は", "次じゃあ", "次私", "何歌", "何の曲", "歌います",
    "喉", "久しぶりに歌", "歌枠", "ごめんなさい", "とか言って", "ご視聴",
)
_PREP_MARKERS = ("歌います", "いきましょう", "歌わせて", "リベンジ", "今年の曲", "流行った曲", "最後だから")
_TITLE_ALIASES = {
    "happynewnyan": ("ハッピーニューニャン", "ハッピーニューニャー", "ハッピーニューニャ"),
}


def _bracketed_titles(text: str) -> list[str]:
    """从摘要正文按出现顺序抽取括号内的曲名/作品名（仅标题，非歌词）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _BRACKET_TITLE.finditer(text or ""):
        t = m.group(1).strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s　\"'`、。，．・!！?？:：;；/／\\\-\[\]（）()【】「」『』《》〈〉]", "", text)


def _title_terms(title: str) -> list[str]:
    terms = [title]
    key = _norm(title)
    terms.extend(_TITLE_ALIASES.get(key, ()))
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        n = _norm(term)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _contains_title(text: str, terms: list[str]) -> bool:
    hay = _norm(text)
    return bool(hay and any(t in hay for t in terms))


def _is_chat_like(text: str) -> bool:
    text = text or ""
    if not text.strip():
        return True
    if len(text.strip()) <= 2:
        return True
    return any(m in text for m in _CHAT_MARKERS)


def _is_prep_like(text: str, terms: list[str]) -> bool:
    return _contains_title(text, terms) or any(m in (text or "") for m in _PREP_MARKERS)


def _parse_section_range(section: dict) -> tuple[float, float] | None:
    tr = (section.get("time_range") or "").split("-")
    if len(tr) != 2:
        return None
    st, en = _ts_to_sec(tr[0].strip()), _ts_to_sec(tr[1].strip())
    if en <= st:
        return None
    return st, en


def _parse_music_entry(text: str) -> tuple[str, str | None] | None:
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"^(.*?)[「『《〈]([^」』》〉]{1,80})[」』》〉]", text)
    if m:
        artist = m.group(1).strip(" \t-—:：/／") or None
        title = m.group(2).strip()
        return (title, artist) if title else None
    titles = _bracketed_titles(text)
    if titles:
        return titles[0], None
    return text, None


def _section_is_opening_bgm(section: dict) -> bool:
    title = (section.get("title_ja") or section.get("title") or "")
    notes = " ".join(str(x) for x in section.get("notes", []) or [])
    blob = " ".join([title, section.get("intro", "") or "", section.get("content", "") or "", notes])
    return "オープニング" in title and ("BGM" in blob or "片段" in blob)


def _songs_from_section_music(summary: dict) -> list[Song]:
    """Prefer the structured section music list over overview-only title splitting.

    The overview often says "including A/B/C" but has no timings. Section music
    entries carry the local time_range, so ASR can then anchor the real singing
    boundary inside that range.
    """
    out: list[Song] = []
    for section in summary.get("sections", []):
        rng = _parse_section_range(section)
        if not rng or _section_is_opening_bgm(section):
            continue
        entries = []
        for raw in section.get("music", []) or []:
            parsed = _parse_music_entry(str(raw))
            if parsed:
                entries.append(parsed)
        if not entries:
            continue
        st, en = rng
        slot = (en - st) / len(entries)
        for i, (title, artist) in enumerate(entries):
            out.append(Song(
                title=title,
                origin="（摘要 music）",
                artist=artist,
                start=st + i * slot,
                end=st + (i + 1) * slot,
                clip_start=st + i * slot,
                clip_end=st + (i + 1) * slot,
                song_start=st + i * slot,
                song_end=st + (i + 1) * slot,
                confidence=0.35,
            ))
    return out


def _section_for_song(summary: dict, song: Song) -> tuple[float, float] | None:
    terms = _title_terms(song.title)
    for section in summary.get("sections", []):
        rng = _parse_section_range(section)
        if not rng or _section_is_opening_bgm(section):
            continue
        for raw in section.get("music", []) or []:
            parsed = _parse_music_entry(str(raw))
            if parsed and _contains_title(parsed[0], terms):
                return rng
    for section in summary.get("sections", []):
        rng = _parse_section_range(section)
        if not rng:
            continue
        st, en = rng
        if st <= song.start < en or st < song.end <= en or (song.start <= st and en <= song.end):
            return rng
    return None


def _per_song_from_overview(summary: dict) -> list[Song]:
    """逐首拆分：摘要总览里的曲名按演唱顺序排列，按时长比例分配到各歌唱小节内。"""
    titles = _bracketed_titles(summary.get("summary", ""))
    sections = summary.get("sections", [])
    secs = []
    for s in sections:
        tr = (s.get("time_range") or "").split("-")
        if len(tr) != 2:
            continue
        title = (s.get("title_ja") or s.get("title") or "")
        if any(h in title for h in _NON_SINGING) and not any(h in title for h in _SING_HINTS):
            continue
        st, en = _ts_to_sec(tr[0].strip()), _ts_to_sec(tr[1].strip())
        if en > st:
            secs.append((st, en, title))
    if not titles or not secs:
        return []
    total = sum(en - st for st, en, _ in secs)
    n = len(titles)
    # 各小节按时长比例分配曲数，余数补到最长小节
    quotas = [max(0, round(n * (en - st) / total)) for st, en, _ in secs]
    while sum(quotas) < n:
        quotas[max(range(len(secs)), key=lambda i: (secs[i][1] - secs[i][0]) / max(quotas[i] + 1, 1))] += 1
    while sum(quotas) > n:
        quotas[max(range(len(quotas)), key=lambda i: quotas[i])] -= 1
    out: list[Song] = []
    ti = 0
    for (st, en, sec_title), q in zip(secs, quotas):
        if q <= 0:
            continue
        slot = (en - st) / q
        for k in range(q):
            if ti >= n:
                break
            out.append(Song(title=titles[ti], origin="（按摘要顺序近似分轨）",
                            start=st + k * slot, end=st + (k + 1) * slot))
            ti += 1
    return out


def _fallback_from_sections(sections: list[dict]) -> list[Song]:
    """LLM 取曲失败时的兜底：用摘要小节（节目自带的分区标题 + 时间区间）作为
    「歌枠ブロック」。只用标题（非歌词），时间来自小节 time_range。"""
    out: list[Song] = []
    for s in sections:
        title = (s.get("title_ja") or s.get("title") or "").strip()
        blob = title + (s.get("intro") or "")
        if not any(h in blob for h in _SING_HINTS):
            continue
        tr = (s.get("time_range") or "").split("-")
        if len(tr) != 2:
            continue
        out.append(Song(title=title or "歌枠ブロック",
                        origin="（摘要分区）",
                        start=_ts_to_sec(tr[0].strip()), end=_ts_to_sec(tr[1].strip())))
    return out


def _format_transcript_segments(segments: Iterable[dict] | None, max_chars: int = 100_000) -> str:
    if not segments:
        return ""
    lines: list[str] = []
    total = 0
    for seg in segments:
        ja = (seg.get("ja") or seg.get("text") or "").strip()
        if not ja:
            continue
        if len(ja) > 140:
            ja = ja[:137] + "..."
        st = _coerce_sec(seg.get("start"))
        en = _coerce_sec(seg.get("end"), st)
        line = f"[{_fmt_time(st)}-{_fmt_time(en)}] {ja}"
        total += len(line) + 1
        if total > max_chars:
            lines.append("...（全程时间轴过长，后续省略；若摘要提到歌曲，请结合摘要保守判断）")
            break
        lines.append(line)
    return "\n".join(lines)


def _fmt_time(sec: float) -> str:
    sec_i = int(max(0, sec))
    return f"{sec_i // 3600:02d}:{(sec_i % 3600) // 60:02d}:{sec_i % 60:02d}"


def _song_from_model_dict(data: dict, fallback: Song | None = None) -> Song | None:
    if not isinstance(data, dict):
        return None
    title = (data.get("title") or (fallback.title if fallback else "") or "").strip()
    if not title:
        return None
    raw_start = _coerce_sec(data.get("start"), fallback.start if fallback else 0.0)
    raw_end = _coerce_sec(data.get("end"), fallback.end if fallback else raw_start)
    song_start = _coerce_sec(data.get("song_start"), fallback.song_start if fallback and fallback.song_start is not None else raw_start)
    song_end = _coerce_sec(data.get("song_end"), fallback.song_end if fallback and fallback.song_end is not None else raw_end)
    clip_start = _coerce_sec(data.get("clip_start"), fallback.clip_start if fallback and fallback.clip_start is not None else raw_start)
    clip_end = _coerce_sec(data.get("clip_end"), fallback.clip_end if fallback and fallback.clip_end is not None else raw_end)
    if song_end <= song_start and raw_end > raw_start:
        song_start, song_end = raw_start, raw_end
    if clip_end <= clip_start:
        clip_start, clip_end = song_start, song_end
    clip_start = min(clip_start, song_start)
    clip_end = max(clip_end, song_end)
    if clip_end <= clip_start:
        return None
    confidence = max(0.0, min(1.0, _coerce_sec(
        data.get("confidence"), fallback.confidence if fallback else 0.0
    )))
    return Song(
        title=title,
        origin=(data.get("origin") or (fallback.origin if fallback else "") or "").strip(),
        artist=(data.get("artist") or (fallback.artist if fallback else None) or "").strip() or None,
        start=clip_start,
        end=clip_end,
        clip_start=clip_start,
        clip_end=clip_end,
        song_start=song_start,
        song_end=song_end,
        confidence=confidence,
    )


def _section_context(summary: dict, song: Song) -> str:
    parts = []
    for s in summary.get("sections", []):
        tr = (s.get("time_range") or "").split("-")
        if len(tr) != 2:
            continue
        st, en = _ts_to_sec(tr[0].strip()), _ts_to_sec(tr[1].strip())
        if st < song.end and song.start < en:
            parts.append(
                f"[{s.get('time_range','')}] {s.get('title_ja','') or s.get('title','')}"
                f"｜{(s.get('intro','') or '')[:220]}"
            )
    return "\n".join(parts)


def _segments_in_window(segments: list[dict], start: float, end: float) -> list[dict]:
    out = []
    for seg in segments:
        st, en = _coerce_sec(seg.get("start")), _coerce_sec(seg.get("end"))
        if en <= start or st >= end:
            continue
        out.append(seg)
    return out


def _next_segment(segs: list[dict], idx: int) -> dict | None:
    return segs[idx + 1] if idx + 1 < len(segs) else None


def _prev_substantial_end(segs: list[dict], start_idx: int, default: float) -> float:
    for j in range(start_idx - 1, -1, -1):
        text = (segs[j].get("ja") or segs[j].get("text") or "").strip()
        if text and len(text) > 2 and not _is_chat_like(text):
            return _coerce_sec(segs[j].get("end"), default)
    return default


def _find_song_start(segs: list[dict], terms: list[str], section_start: float) -> tuple[float, int]:
    title_idx: int | None = None
    for i, seg in enumerate(segs):
        text = seg.get("ja") or seg.get("text") or ""
        if _contains_title(text, terms):
            title_idx = i
            break
    if title_idx is not None:
        seg = segs[title_idx]
        st = _coerce_sec(seg.get("start"))
        en = _coerce_sec(seg.get("end"), st)
        nxt = _next_segment(segs, title_idx)
        if nxt is not None:
            nst = _coerce_sec(nxt.get("start"))
            # Short "song title" callouts are usually followed by the first lyric.
            if (en - st) <= 12.0 and 0.0 <= (nst - en) <= 20.0:
                return nst, title_idx + 1
        return st, title_idx

    # No title callout in ASR: use the first non-chat line after the rough section
    # start. This catches songs whose title was omitted/misrecognized by ASR.
    for i, seg in enumerate(segs):
        st = _coerce_sec(seg.get("start"))
        text = seg.get("ja") or seg.get("text") or ""
        if st < section_start - 1:
            continue
        if _is_chat_like(text) or any(m in text for m in _PREP_MARKERS):
            continue
        return st, i
    return section_start, 0


def _find_song_end(segs: list[dict], start_idx: int, song_start: float,
                   terms: list[str], section_end: float) -> float:
    last_music_end = song_start
    min_song_end = song_start + 90.0
    for i in range(start_idx, len(segs)):
        seg = segs[i]
        st = _coerce_sec(seg.get("start"))
        en = _coerce_sec(seg.get("end"), st)
        if st < song_start - 1:
            continue
        text = (seg.get("ja") or seg.get("text") or "").strip()
        nxt = _next_segment(segs, i)
        next_text = (nxt.get("ja") or nxt.get("text") or "") if nxt else ""

        if st >= min_song_end and _contains_title(text, terms) and "でした" in text:
            return en

        if st >= min_song_end and "ご視聴" in text and (nxt is None or _is_chat_like(next_text)):
            return max(last_music_end, _prev_substantial_end(segs, i, last_music_end))

        if st >= min_song_end and "ご視聴" not in text and _is_chat_like(text):
            return max(last_music_end, _prev_substantial_end(segs, i, last_music_end))

        if text and not _is_chat_like(text) and "ご視聴" not in text:
            last_music_end = max(last_music_end, en)

    return min(section_end, max(last_music_end, song_start + 120.0))


def _find_clip_start(segs: list[dict], song_start: float, terms: list[str],
                     section_start: float) -> float:
    clip_start = max(section_start, song_start - 10.0)
    for seg in reversed(segs):
        st = _coerce_sec(seg.get("start"))
        if st < song_start - 45.0 or st >= song_start:
            continue
        text = seg.get("ja") or seg.get("text") or ""
        if _is_prep_like(text, terms):
            clip_start = max(section_start, st)
            break
    return clip_start


def _find_clip_end(segs: list[dict], song_end: float, section_end: float) -> float:
    clip_end = min(section_end, song_end + 20.0)
    for seg in segs:
        st = _coerce_sec(seg.get("start"))
        en = _coerce_sec(seg.get("end"), st)
        if st < song_end or st > song_end + 30.0:
            continue
        text = seg.get("ja") or seg.get("text") or ""
        if _is_chat_like(text):
            clip_end = min(section_end, en)
            break
    return max(song_end, clip_end)


def _refine_song_from_asr(summary: dict, song: Song, segments: list[dict]) -> Song:
    section_start, section_end = song.start, song.end
    section_rng = _section_for_song(summary, song)
    if section_rng:
        section_start, section_end = section_rng

    segs = _segments_in_window(segments, max(0.0, section_start - 600.0), section_end + 120.0)
    if not segs:
        return song
    terms = _title_terms(song.title)
    song_start, start_idx = _find_song_start(segs, terms, section_start)
    song_end = _find_song_end(segs, start_idx, song_start, terms, section_end)
    if song_end <= song_start:
        return song
    clip_start = _find_clip_start(segs, song_start, terms, section_start)
    clip_end = _find_clip_end(segs, song_end, section_end)
    if clip_end <= clip_start:
        clip_start, clip_end = song_start, song_end
    return Song(
        title=song.title,
        origin=song.origin,
        artist=song.artist,
        start=clip_start,
        end=clip_end,
        clip_start=clip_start,
        clip_end=clip_end,
        song_start=song_start,
        song_end=song_end,
        confidence=max(song.confidence, 0.72),
    )


def _refine_songs_from_asr(summary: dict, songs: list[Song], segments: list[dict]) -> list[Song]:
    if not songs or not segments:
        return songs
    return [_refine_song_from_asr(summary, song, segments) for song in songs]


def _refine_song_windows(summary: dict, songs: list[Song], segments: list[dict],
                         llm: LLMClient) -> list[Song]:
    if not songs or not segments:
        return songs
    songs = _refine_songs_from_asr(summary, songs, segments)
    refined: list[Song] = []
    overview = (summary.get("summary", "") or "")[:800]
    for song in songs:
        song_dur = (song.song_end if song.song_end is not None else song.end) - (
            song.song_start if song.song_start is not None else song.start
        )
        if song.confidence >= 0.7 and 60.0 <= song_dur <= 420.0:
            refined.append(song)
            continue
        window_start = max(0.0, song.start - 120.0)
        window_end = song.end + 120.0
        window = _format_transcript_segments(
            _segments_in_window(segments, window_start, window_end),
            max_chars=18_000,
        )
        if not window:
            refined.append(song)
            continue
        payload = (
            f"候选：{song.title}｜{song.origin}｜粗略切片 {_fmt_time(song.start)}-{_fmt_time(song.end)}\n"
            f"总览：{overview}\n"
            f"相关小节：\n{_section_context(summary, song)}\n\n"
            f"候选附近 ASR 时间轴：\n{window}"
        )
        try:
            data = llm.complete_json(_REFINE_SYSTEM, payload, max_tokens=1200)
        except Exception as e:  # noqa: BLE001 — 单曲精修失败时保留候选，不阻断整场
            print(f"  [warn] 单曲时间精修失败（{song.title}）：{e}")
            refined.append(song)
            continue
        item = data.get("song") if isinstance(data, dict) else None
        next_song = _song_from_model_dict(item or data, fallback=song)
        if next_song is None:
            refined.append(song)
            continue
        if next_song.end < window_start or next_song.start > window_end:
            refined.append(song)
            continue
        refined.append(next_song)
    return refined


def extract_setlist(
    summary: dict,
    llm: LLMClient,
    transcript_segments: Iterable[dict] | None = None,
) -> list[Song]:
    sections = summary.get("sections", [])
    transcript_segments = list(transcript_segments or [])
    # 用「总览 + 小节标题/时间/intro 描述 + 全程 ASR 时间轴」认曲名和歌曲正文边界。
    # 只要求模型输出曲名与时间，不输出歌词内容。
    payload_lines = []
    for s in sections:
        payload_lines.append(
            f"[{s.get('time_range','')}] {s.get('title_ja','') or s.get('title','')}"
            f"｜{(s.get('intro','') or '')[:160]}"
        )
    transcript = _format_transcript_segments(transcript_segments, max_chars=35_000)
    payload = (
        "总览：" + (summary.get("summary", "") or "")[:1800]
        + "\n\n小节：\n" + "\n".join(payload_lines)
        + ("\n\n全程 ASR 时间轴（只用于判断曲名和时间，不要复述歌词）：\n" + transcript if transcript else "")
    )
    try:
        data = llm.complete_json(_SYSTEM, payload, max_tokens=8192)
    except Exception as e:  # noqa: BLE001 — LLM 取曲失败 → 逐首拆分(摘要曲名) → 分区兜底
        print(f"  [warn] setlist LLM 失败，使用摘要 music + ASR 时间轴兜底：{e}")
        out = _songs_from_section_music(summary) or _per_song_from_overview(summary) or _fallback_from_sections(sections)
        return _refine_song_windows(summary, out, transcript_segments, llm)
    out: list[Song] = []
    for s in data.get("songs", []):
        song = _song_from_model_dict(s)
        if song:
            out.append(song)
    if not out:
        out = _songs_from_section_music(summary) or _per_song_from_overview(summary) or _fallback_from_sections(sections)
    return _refine_song_windows(summary, out, transcript_segments, llm)
