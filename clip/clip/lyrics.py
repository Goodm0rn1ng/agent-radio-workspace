"""歌枠字幕处理。

四档策略：
- ``metadata``：默认。唱歌期间只显示曲名/原唱；曲目信息优先用 setlist LLM 结果，
  必要时用歌唱 ASR 片段生成检索 query 并从用户配置的 Netease 服务取规范元数据。
- ``netease``：调用用户自有/已授权的 NeteaseCloudMusicApi 服务，取整首歌原文歌词
  与中文翻译，直接覆盖歌唱区间；ASR 只用于曲名检索兜底和边界，不再作为歌唱正文。
- ``file``：只使用 ``SongSpan.lyrics_file`` 指向的用户自备/已授权 .srt/.lrc。
- ``placeholder``：不输出歌词正文，只显示「♪ 曲名 ♪」占位。

本模块只连接调用方配置的服务，不内置公开镜像地址。歌词授权、账号/cookie 与可展示范围由
调用方在部署侧保证。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

import httpx

from clip.aligner import Cue, parse_srt, translate_lines
from clip.config import clip_config


@dataclass
class SongSpan:
    start: float                  # 绝对秒
    end: float
    title: str                    # 曲名/标注（不含歌词）
    lyrics_file: str | None = None  # 可选：用户自备/已授权歌词文件（.srt 或 .lrc）
    artist: str | None = None


@dataclass
class LyricsLine:
    start: float
    end: float
    ja: str
    zh: str = ""


@dataclass
class LyricsBundle:
    provider: str
    song_id: str
    title: str
    artist: str
    lines: list[LyricsLine]


@dataclass
class SongMetadata:
    title: str
    artist: str | None = None
    provider: str = ""
    song_id: str | None = None


_LRC_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
_YRC_ROW = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_YRC_WORD = re.compile(r"\(\d+,\d+,\d+\)")
_BRACKETED_TITLE = re.compile(r"[「『【\[]([^」』】\]]+)[」』】\]]")
_GENERIC_TITLES = {
    "歌", "歌曲", "歌枠", "歌枠ブロック", "演唱", "翻唱", "cover", "カバー",
    "メドレー", "song", "songs", "live", "直播", "配信", "切片", "切り抜き",
}
_CREDIT_PREFIXES = (
    "作詞", "作词", "作曲", "編曲", "编曲", "編成", "制作人", "作詞/作曲",
    "词：", "曲：", "词:", "曲:", "lyricist", "composer", "arranger",
)
_OFFICIAL_CORRECTION_MIN_SCORE = 0.90


def load_lyrics(path: str | Path) -> list[Cue]:
    """读取用户提供的歌词文件 → Cue 列表（相对其自身 0 点）。支持 .srt / .lrc。
    纯格式解析，不含任何歌词内容；内容完全来自用户文件。"""
    p = Path(path)
    if p.suffix.lower() == ".srt":
        return parse_srt(p)
    return [
        Cue(x.start, x.end, x.ja, x.zh)
        for x in _entries_to_lines(parse_lrc_text(p.read_text(encoding="utf-8")), skip_credits=False)
    ]


def parse_lrc_text(text: str) -> list[tuple[float, str]]:
    """.lrc：[mm:ss.xx]文本，多时间戳行展开。"""
    entries: list[tuple[float, str]] = []
    for line in text.splitlines():
        stamps = _LRC_TS.findall(line)
        text = _LRC_TS.sub("", line).strip()
        if not stamps:
            continue
        for mm, ss, frac in stamps:
            t = int(mm) * 60 + int(ss) + (int(frac[:3].ljust(3, "0")) / 1000 if frac else 0)
            entries.append((t, text))
    return sorted(entries, key=lambda e: e[0])


def parse_yrc_text(text: str) -> list[tuple[float, str]]:
    """解析 NeteaseCloudMusicApi `/lyric/new` 可能返回的 yrc 行级时间戳。"""
    entries: list[tuple[float, str]] = []
    for raw in text.splitlines():
        if raw.lstrip().startswith("{"):
            continue
        m = _YRC_ROW.match(raw.strip())
        if not m:
            continue
        lyric = _YRC_WORD.sub("", m.group(3)).strip()
        entries.append((int(m.group(1)) / 1000.0, lyric))
    return sorted(entries, key=lambda e: e[0])


def _entries_to_lines(
    entries: list[tuple[float, str]],
    translations: list[tuple[float, str]] | None = None,
    *,
    skip_credits: bool = True,
) -> list[LyricsLine]:
    clean = [
        (t, s.strip()) for t, s in entries
        if s.strip() and (not skip_credits or _is_display_lyric(s))
    ]
    if not clean:
        return []
    translations = [(t, s.strip()) for t, s in (translations or []) if s.strip()]
    lines: list[LyricsLine] = []
    for i, (t, text) in enumerate(clean):
        end = clean[i + 1][0] if i + 1 < len(clean) else t + 4.0
        zh = _nearest_translation(t, translations)
        lines.append(LyricsLine(start=t, end=max(end, t + 0.5), ja=text, zh=zh))
    return lines


def _nearest_translation(t: float, translations: list[tuple[float, str]]) -> str:
    if not translations:
        return ""
    best_t, best_text = min(translations, key=lambda x: abs(x[0] - t))
    return best_text if abs(best_t - t) <= 1.2 else ""


def _is_display_lyric(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    lowered = s.lower()
    return not any(lowered.startswith(p.lower()) for p in _CREDIT_PREFIXES)


def _lyric_mode() -> str:
    mode = (clip_config.lyrics_mode or "placeholder").strip().lower()
    return mode if mode in {"metadata", "netease", "file", "placeholder"} else "metadata"


def _normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#＃]\S+", "", text)
    return re.sub(r"[\s\u3000、。，．,.!?！？「」『』♪・…〜~\-＿_（）()［］【】\[\]{}<>《》〈〉:：;；\"'“”‘’\\|]+", "", text)


def _similarity(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return max(0.86, SequenceMatcher(None, na, nb).ratio())
    return SequenceMatcher(None, na, nb).ratio()


def _expanded_title_candidates(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw:
        return []
    pieces = [m.group(1).strip() for m in _BRACKETED_TITLE.finditer(raw)]
    pieces.append(raw)
    for sep in ("｜", "|", " - ", " – ", " — ", "／"):
        for piece in list(pieces):
            pieces.extend(p.strip() for p in piece.split(sep) if p.strip())
    out: list[str] = []
    for p in pieces:
        p = re.sub(r"(?i)\b(cover|covered by|karaoke)\b", "", p)
        p = re.sub(r"(歌ってみた|カバー|翻唱|演唱|切り抜き|切片|歌枠)", "", p)
        p = p.strip(" -_｜|/　")
        if _is_specific_title(p) and p not in out:
            out.append(p)
    return out


def _is_specific_title(title: str) -> bool:
    norm = _normalize(title)
    return len(norm) >= 2 and norm not in {_normalize(x) for x in _GENERIC_TITLES}


def fetch_authorized_lyrics_for_candidates(
    candidates: Iterable[str],
    artist: str | None = None,
) -> LyricsBundle | None:
    """按候选曲名查授权网易云歌词。非 netease 模式直接返回 None。"""
    if _lyric_mode() != "netease":
        return None
    seen: set[str] = set()
    for raw in candidates:
        for title in _expanded_title_candidates(raw):
            key = _normalize(title)
            if key in seen:
                continue
            seen.add(key)
            bundle = _fetch_netease_lyrics(title, artist=artist)
            if not bundle and artist:
                bundle = _fetch_netease_lyrics(title, artist=None)
            if bundle:
                return bundle
    return None


def resolve_song_metadata_for_candidates(
    candidates: Iterable[str],
    artist: str | None = None,
) -> SongMetadata | None:
    """按候选曲名检索规范曲名/artist。只取元数据，不取歌词正文。"""
    seen: set[str] = set()
    for raw in candidates:
        for title in _expanded_title_candidates(raw):
            key = _normalize(title)
            if key in seen:
                continue
            seen.add(key)
            song = _search_netease_song(title, artist=artist)
            if not song and artist:
                song = _search_netease_song(title, artist=None)
            if song:
                return SongMetadata(
                    title=str(song.get("name") or title),
                    artist=", ".join(_artist_names(song)) or artist,
                    provider="netease",
                    song_id=str(song.get("id") or ""),
                )
    return None


def resolve_song_metadata_for_span(
    span: SongSpan,
    asr_cues: list[Cue],
    llm=None,
) -> SongMetadata:
    """歌唱区间显示用曲目信息：优先检索规范元数据，失败回退 setlist 结果。"""
    candidates: list[str] = []
    for q in _llm_search_candidates_from_asr(span, asr_cues, llm=llm):
        if q and q not in candidates:
            candidates.append(q)
    if span.title and span.title not in candidates:
        candidates.append(span.title)
    meta = resolve_song_metadata_for_candidates(candidates, artist=span.artist)
    if meta:
        return meta
    return SongMetadata(title=span.title or "歌曲", artist=span.artist)


def _fetch_netease_lyrics(title: str, artist: str | None = None) -> LyricsBundle | None:
    song = _search_netease_song(title, artist=artist)
    if not song:
        return None
    data = _netease_json("/lyric/new", {"id": str(song["id"])})
    if not data:
        data = _netease_json("/lyric", {"id": str(song["id"])})
    if not data:
        return None

    lrc = _lyric_text(data, "lrc")
    tlyric = _lyric_text(data, "tlyric")
    yrc = _lyric_text(data, "yrc")
    entries = parse_lrc_text(lrc) or parse_yrc_text(yrc)
    lines = _entries_to_lines(entries, parse_lrc_text(tlyric))
    if not lines:
        return None
    return LyricsBundle(
        provider="netease",
        song_id=str(song["id"]),
        title=str(song.get("name") or title),
        artist=", ".join(_artist_names(song)),
        lines=lines,
    )


def _netease_json(path: str, params: dict[str, str]) -> dict | None:
    base = (clip_config.lyrics_netease_base_url or "").strip().rstrip("/")
    if not base:
        return None
    req_params = dict(params)
    if clip_config.lyrics_netease_cookie:
        req_params["cookie"] = clip_config.lyrics_netease_cookie
    try:
        with httpx.Client(timeout=clip_config.lyrics_netease_timeout_sec, follow_redirects=True) as client:
            resp = client.get(f"{base}{path}", params=req_params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _search_netease_song(title: str, artist: str | None = None) -> dict | None:
    songs = _search_netease_song_candidates(title, artist)
    if not songs:
        return None
    if artist:
        artist_norm = _normalize(artist)
        artist_matches = [
            s for s in songs
            if artist_norm and artist_norm in _normalize(" ".join(_artist_names(s)))
        ]
        if artist_matches:
            songs = artist_matches
        else:
            return None
    best = max(songs, key=lambda s: _song_score(title, artist, s))
    return best if _song_score(title, artist, best) >= clip_config.lyrics_netease_match_score else None


def _search_netease_song_candidates(title: str, artist: str | None = None) -> list[dict]:
    queries: list[str] = []
    if artist:
        queries.extend([f"{title} {artist}", f"{artist} {title}"])
    queries.append(title)

    seen_queries: set[str] = set()
    seen_ids: set[str] = set()
    out: list[dict] = []
    limit = max(1, clip_config.lyrics_search_limit, 10 if artist else 1)
    for query in queries:
        key = _normalize(query)
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        params = {"keywords": query, "type": "1", "limit": str(limit)}
        for path in ("/cloudsearch", "/search"):
            data = _netease_json(path, params)
            result = data.get("result", {}) if data else {}
            songs = [s for s in (result.get("songs") or []) if isinstance(s, dict) and s.get("id")]
            if songs:
                for song in songs:
                    song_id = str(song.get("id"))
                    if song_id in seen_ids:
                        continue
                    seen_ids.add(song_id)
                    out.append(song)
                break
    return out


def _song_score(title: str, artist: str | None, song: dict) -> float:
    score = _similarity(title, str(song.get("name") or ""))
    if artist:
        artist_norm = _normalize(artist)
        names = _normalize(" ".join(_artist_names(song)))
        if artist_norm and artist_norm in names:
            score = min(1.0, score + 0.08)
    return score


def _artist_names(song: dict) -> list[str]:
    people = song.get("ar") or song.get("artists") or []
    return [str(p.get("name") or "") for p in people if isinstance(p, dict) and p.get("name")]


def _lyric_text(data: dict, key: str) -> str:
    obj = data.get(key) or {}
    return str(obj.get("lyric") or "") if isinstance(obj, dict) else ""


def merge_asr_cues_with_authorized_lyrics(
    cues: list[Cue],
    title_candidates: Iterable[str],
) -> tuple[list[Cue], str | None]:
    """普通候选切片：只在 90% 以上相似时，用授权歌词保守纠错。"""
    bundle = fetch_authorized_lyrics_for_candidates(title_candidates)
    if not bundle:
        return cues, None
    merged, scores = _merge_cues_sequential(cues, bundle.lines)
    if not scores:
        return cues, None
    return merged, f"网易云歌词({bundle.title}, 保守纠错{len(scores)}句)"


def _merge_cues_sequential(cues: list[Cue], lines: list[LyricsLine]) -> tuple[list[Cue], list[float]]:
    merged: list[Cue] = []
    scores: list[float] = []
    cursor = 0
    threshold = _official_correction_threshold()
    for cue in cues:
        if not cue.ja:
            merged.append(cue)
            continue
        best_i, best_score = _best_line_index(cue.ja, lines, cursor)
        if best_i >= 0 and best_score >= threshold:
            line = lines[best_i]
            merged.append(_merged_cue(cue, line))
            scores.append(best_score)
            cursor = best_i + 1
        else:
            merged.append(cue)
    return merged, scores


def _best_line_index(text: str, lines: list[LyricsLine], cursor: int) -> tuple[int, float]:
    lo = max(0, cursor - 2)
    hi = min(len(lines), cursor + 18)
    best_i, best_score = -1, 0.0
    for i in range(lo, hi):
        score = _similarity(text, lines[i].ja)
        if score > best_score:
            best_i, best_score = i, score
    return best_i, best_score


def _merged_cue(cue: Cue, line: LyricsLine) -> Cue:
    return Cue(cue.start, cue.end, line.ja, line.zh or cue.zh)


def _official_correction_threshold() -> float:
    return max(_OFFICIAL_CORRECTION_MIN_SCORE, clip_config.lyrics_official_min_score)


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def anchor_cues_are_usable(anchor_cues: list[Cue], duration: float) -> bool:
    """短片二次识别只在锚点足够密时参与校时，避免稀疏 ASR 把时间轴拉歪。"""
    if not anchor_cues or duration <= 0:
        return False
    min_count = max(1, int(duration / 12))
    if len(anchor_cues) < min_count:
        return False
    long_cues = [c for c in anchor_cues if (c.end - c.start) > 14]
    return len(long_cues) <= max(1, len(anchor_cues) // 4)


def _repair_talk_zh(
    cues: list[Cue],
    llm,
    terminology: str = "",
    *,
    force: bool = False,
    translation_prompt_path=None,
) -> None:
    """修复谈话字幕的中文译文：复用原转写译文（按时间重叠）会产生两类瑕疵——
    ① 二次精听把一句拆成多条短字幕时，多条复制同一句译文（一大段相同译文）；
    ② 重识别处原转写无对应译文 → 整条缺中文。

    这里检出「空译」与「与相邻条同译（重复段）」的谈话条，用 LLM 逐条重译填补。
    二次精听可用时调用方可 force=True，让 LLM 基于修正后的 ASR 文本整批重译。
    失败时静默保留原值。
    """
    if llm is None:
        return
    n = len(cues)
    if n == 0:
        return

    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    if force:
        todo = [i for i, c in enumerate(cues) if c.ja.strip()]
    else:
        todo: list[int] = []
        for i, c in enumerate(cues):
            if not c.ja.strip():
                continue
            z = norm(c.zh)
            if not z:
                todo.append(i)
                continue
            dup = ((i > 0 and norm(cues[i - 1].zh) == z)
                   or (i < n - 1 and norm(cues[i + 1].zh) == z))
            if dup:
                todo.append(i)
    if not todo:
        return
    zh = translate_lines(
        [cues[i].ja for i in todo],
        llm=llm,
        terminology=terminology,
        template_path=translation_prompt_path,
        prior_zh=[cues[i].zh for i in todo],
    )
    fixed = 0
    for k, i in enumerate(todo):
        if k < len(zh) and zh[k].strip():
            cues[i].zh = zh[k].strip()
            fixed += 1
    if fixed:
        mode = "二次精听后重译" if force else "补空译/拆重复"
        print(f"  译文修复：{fixed}/{len(todo)} 条（{mode}）")


def build_clip_cues(segments: list[dict], clip_start: float, clip_end: float,
                    song_spans: list[SongSpan] | None = None,
                    anchor_cues: list[Cue] | None = None,
                    llm=None, terminology: str = "",
                    translation_prompt_path=None,
                    force_llm_retranslate: bool = False) -> list[Cue]:
    """把绝对时间的双语转写句裁成 clip 相对时间的 Cue。

    segments: [{start,end,ja,zh}, ...]（绝对秒）。
    歌唱区间内的句子被占位/已授权歌词替代；谈话句保留中日。
    anchor_cues: 对切出的短片重新识别得到的相对时间 Cue；只作为精细时间锚，
    不直接替换字幕文本。
    """
    song_spans = song_spans or []
    origin = clip_start
    cues: list[Cue] = []
    anchor_cues = anchor_cues or []

    # 1) 谈话句（不落在任何歌唱区间内）→ 中日字幕
    relisten = ((clip_config.whisperx_relisten_text or force_llm_retranslate)
                and anchor_cues_are_usable(anchor_cues, clip_end - clip_start))
    if relisten:
        # 二次精听：字幕覆盖跟随「重识别检测到语音的地方」——把词级结果按停顿成句，
        # 哪里有语音哪里就有字幕（治「有语音/无字幕」），文本用重识别（VAD+去幻觉，治幻觉），
        # 中文按时间重叠复用原转写译文。歌唱区间内的词跳过（交由歌唱策略）。
        talk_cues = _talk_cues_from_words(anchor_cues, segments, song_spans,
                                          clip_start, clip_end, origin, llm=llm)
        if not talk_cues:   # 重识别意外为空 → 退回原转写，避免整段无字幕
            talk_cues = _talk_cues_from_segments(segments, song_spans, clip_start, clip_end, origin)
    else:
        talk_cues = []
        for seg in segments:
            st, en = float(seg.get("start", 0)), float(seg.get("end", 0))
            if en <= clip_start or st >= clip_end:
                continue
            if any(_overlaps(st, en, sp.start, sp.end) for sp in song_spans):
                continue
            talk_cues.append(Cue(st - origin, en - origin,
                                 (seg.get("ja") or "").strip(), (seg.get("zh") or "").strip()))
        if anchor_cues_are_usable(anchor_cues, clip_end - clip_start):
            talk_cues = _retime_cues_with_anchors(talk_cues, anchor_cues)
    _repair_talk_zh(
        talk_cues,
        llm,
        terminology,
        force=relisten or force_llm_retranslate,
        translation_prompt_path=translation_prompt_path,
    )
    cues.extend(talk_cues)

    # 2) 歌唱区间 → 歌曲信息 / 已授权歌词文件 / 授权网易云歌词 / 占位（始终覆盖，不依赖转写句）
    for span in song_spans:
        if not _overlaps(span.start, span.end, clip_start, clip_end):
            continue
        vis0, vis1 = max(span.start, clip_start), min(span.end, clip_end)
        mode = _lyric_mode()
        if mode == "file":
            if span.lyrics_file and Path(span.lyrics_file).exists():
                for c in load_lyrics(span.lyrics_file):
                    cues.append(Cue(c.start + (span.start - origin),
                                    c.end + (span.start - origin), c.ja, c.zh))
            else:
                cues.append(_placeholder_song_cue(span, vis0, vis1, origin))
        elif mode == "netease":
            if span.lyrics_file and Path(span.lyrics_file).exists():
                for c in load_lyrics(span.lyrics_file):
                    cues.append(Cue(c.start + (span.start - origin),
                                    c.end + (span.start - origin), c.ja, c.zh))
                continue
            span_anchors = _anchor_cues_in_span(anchor_cues, span, origin)
            if not anchor_cues_are_usable(span_anchors, span.end - span.start):
                span_anchors = []
            asr_cues = span_anchors or _span_asr_cues(segments, span, origin)
            span_cues = _netease_span_cues(span, asr_cues, clip_start, clip_end, origin, llm=llm)
            if span_cues:
                cues.extend(span_cues)
            else:
                cues.append(_song_metadata_cue(span, asr_cues, vis0, vis1, origin, llm=llm))
        elif mode == "metadata":
            span_anchors = _anchor_cues_in_span(anchor_cues, span, origin)
            if not anchor_cues_are_usable(span_anchors, span.end - span.start):
                span_anchors = []
            asr_cues = span_anchors or _span_asr_cues(segments, span, origin)
            cues.append(_song_metadata_cue(span, asr_cues, vis0, vis1, origin, llm=llm))
        else:
            cues.append(_placeholder_song_cue(span, vis0, vis1, origin))

    # 安全网：把所有字幕严格夹到本片 [0, 片长] 内、丢弃越界句——避免「区间型歌曲块」
    # 等路径泄漏本片范围以外的字幕（残留以前数据 / 不从 0 开始）。
    clip_dur = clip_end - clip_start
    clamped: list[Cue] = []
    for c in cues:
        s, e = max(0.0, c.start), min(clip_dur, c.end)
        if e - s > 0.05:
            clamped.append(Cue(s, e, c.ja, c.zh))
    clamped.sort(key=lambda c: c.start)
    return clamped


def _placeholder_song_cue(span: SongSpan, vis0: float, vis1: float, origin: float) -> Cue:
    title = (span.title or "歌曲").strip()
    artist = (span.artist or "").strip()
    zh = f"原唱：{artist}" if artist else "歌曲演唱中"
    return Cue(vis0 - origin, vis1 - origin, f"♪ {title} ♪", zh)


def _song_metadata_cue(
    span: SongSpan,
    asr_cues: list[Cue],
    vis0: float,
    vis1: float,
    origin: float,
    llm=None,
) -> Cue:
    meta = resolve_song_metadata_for_span(span, asr_cues, llm=llm)
    title = (meta.title or span.title or "歌曲").strip()
    artist = (meta.artist or span.artist or "").strip()
    zh = f"原唱：{artist}" if artist else "歌曲演唱中"
    return Cue(vis0 - origin, vis1 - origin, f"♪ {title} ♪", zh)


def _best_zh_overlap(abs_start: float, abs_end: float, segments: list[dict]) -> str:
    """取与 [abs_start,abs_end] 时间重叠最多的原转写段的中文译文（复用已有翻译）。"""
    best_zh, best_ov = "", 0.0
    for seg in segments:
        st, en = float(seg.get("start", 0)), float(seg.get("end", 0))
        ov = min(abs_end, en) - max(abs_start, st)
        zh = (seg.get("zh") or "").strip()
        if ov > best_ov and zh:
            best_ov, best_zh = ov, zh
    return best_zh


def _talk_cues_from_segments(segments: list[dict], song_spans: list[SongSpan],
                             clip_start: float, clip_end: float, origin: float) -> list[Cue]:
    """退路：直接用原转写的谈话句（不在歌唱区间内）。"""
    out: list[Cue] = []
    for seg in segments:
        st, en = float(seg.get("start", 0)), float(seg.get("end", 0))
        if en <= clip_start or st >= clip_end:
            continue
        if any(_overlaps(st, en, sp.start, sp.end) for sp in song_spans):
            continue
        out.append(Cue(st - origin, en - origin,
                       (seg.get("ja") or "").strip(), (seg.get("zh") or "").strip()))
    return out


def _talk_cues_from_words(anchor_words: list[Cue], segments: list[dict],
                          song_spans: list[SongSpan], clip_start: float, clip_end: float,
                          origin: float, gap: float = 0.7, max_dur: float = 4.0,
                          max_chars: int = 20, llm=None) -> list[Cue]:
    """字幕跟随语音：把短片重识别的词级结果（绝对时间）按停顿/长度成句。

    覆盖严格落在「重识别检测到语音」处——有语音即有字幕，无语音（VAD 静音）即无字幕，
    从而既治「有语音/无字幕」又避免静音处幻觉。歌唱区间内的词跳过。中文复用原转写按时间重叠。
    """
    aw = sorted(((w.start + origin, w.end + origin, (w.ja or "").strip()) for w in anchor_words),
                key=lambda x: x[0])
    aw = [(s, e, t) for s, e, t in aw
          if t and e > clip_start and s < clip_end
          and not any(_overlaps(s, e, sp.start, sp.end) for sp in song_spans)]
    if llm is not None:
        # LLM 断句仅作「更自然的成句」加分项：成功就用，失败/不可靠即退回下方停顿断句，
        # 绝不死等。译文修复（_repair_talk_zh）是另一独立步骤，不受这里退回影响。
        try:
            llm_cues = _llm_segment_talk_words(aw, segments, origin, llm)
            if llm_cues:
                print(f"  LLM断句：{len(aw)} 词 -> {len(llm_cues)} 句")
                return _repair_japanese_word_splits(llm_cues)
            print("  [warn] LLM断句未返回有效结果，退回停顿断句（不影响 LLM 重译）")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] LLM断句失败，退回停顿断句（不影响 LLM 重译）：{e}")

    cues: list[Cue] = []
    group: list[tuple[float, float, str]] = []

    def flush() -> None:
        if not group:
            return
        s0, e0 = group[0][0], group[-1][1]
        ja = "".join(t for _, _, t in group)
        if ja:
            cues.append(Cue(s0 - origin, max(e0 - origin, s0 - origin + 0.4),
                            ja, _best_zh_overlap(s0, e0, segments)))
        group.clear()

    for s, e, t in aw:
        if group:
            prev_end = group[-1][1]
            dur = e - group[0][0]
            chars = sum(len(x[2]) for x in group)
            if (s - prev_end) > gap or dur > max_dur or chars >= max_chars:
                flush()
        group.append((s, e, t))
    flush()
    return _repair_japanese_word_splits(cues)


_LLM_SEGMENT_SYSTEM = """你是日语直播切片字幕断句助手。输入是二次精听 ASR 的 token 时间轴，已排除唱歌区间；请先整体阅读全部 token，再按自然阅读习惯切成字幕句。

任务只做断句，不翻译，不补写歌词，不大幅改写 ASR。

规则：
- 每条字幕应是一个自然短句、回应、呼喊或语义片段，避免把上一句结尾和下一句开头拼在一起。
- 口语直播中，评论名/感谢、话题转换、感叹、解释说明通常要拆开。
- 不要让字幕停在明显需要后续内容的词上；除非已经到最后一个 token。
- 避免输出把前一句中段、重复口癖和下一句开头混在一起的句子。
- 一条字幕尽量 6-18 个日文字符，最长不要超过 24 个字符；很短的“うん/はい/えー”等可单独成句。
- 必须覆盖所有 token，按顺序输出，不能重叠、不能跳过、不能改变 token 顺序。
- start_i 和 end_i 都是输入 token 的 i，end_i 为包含式索引。

严格输出 JSON：
{"segments":[{"start_i":0,"end_i":3},{"start_i":4,"end_i":7}]}"""


def _llm_segment_talk_words(
    words: list[tuple[float, float, str]],
    segments: list[dict],
    origin: float,
    llm,
) -> list[Cue]:
    if not words:
        return []
    ranges = _llm_segment_ranges_with_retry(words, llm, max_tokens=8192, label="整段")
    if not ranges and len(words) > _LLM_SEGMENT_MIN_CHUNK_WORDS:
        print(f"  [warn] LLM断句整段失败，改用分块断句：{len(words)} 词")
        ranges = _llm_segment_ranges_chunked(words, llm)
    if not ranges:
        return []
    ranges = _fill_missing_token_ranges(ranges, len(words))
    ranges = _split_long_token_ranges(ranges, words)

    cues: list[Cue] = []
    for first, last in ranges:
        s0, e0 = words[first][0], words[last][1]
        ja = "".join(t for _, _, t in words[first:last + 1]).strip()
        if ja:
            cues.append(Cue(s0 - origin, max(e0 - origin, s0 - origin + 0.4),
                            ja, _best_zh_overlap(s0, e0, segments)))
    return cues


_LLM_SEGMENT_RETRIES = 2
_LLM_SEGMENT_CHUNK_WORDS = 1200
_LLM_SEGMENT_MIN_CHUNK_WORDS = 32
_SUBTITLE_HARD_MAX_CHARS = 24
_SUBTITLE_HARD_MAX_DUR = 4.0


def _split_long_token_ranges(
    ranges: list[tuple[int, int]],
    words: list[tuple[float, float, str]],
    *,
    max_chars: int = _SUBTITLE_HARD_MAX_CHARS,
    max_dur: float = _SUBTITLE_HARD_MAX_DUR,
) -> list[tuple[int, int]]:
    """Hard guard after LLM segmentation so one cue cannot become a paragraph."""
    out: list[tuple[int, int]] = []
    for first, last in ranges:
        cur = first
        while cur <= last:
            start = cur
            chars = 0
            prev = cur
            while cur <= last:
                next_chars = chars + len(words[cur][2])
                next_dur = words[cur][1] - words[start][0]
                if cur > start and (next_chars > max_chars or next_dur > max_dur):
                    break
                chars = next_chars
                prev = cur
                cur += 1
            out.append((start, prev))
            cur = prev + 1
    return out


def _llm_segment_ranges_with_retry(
    words: list[tuple[float, float, str]],
    llm,
    *,
    max_tokens: int,
    label: str,
) -> list[tuple[int, int]]:
    errors: list[str] = []
    for attempt in range(1, _LLM_SEGMENT_RETRIES + 1):
        try:
            ranges = _llm_segment_ranges_once(words, llm, max_tokens=max_tokens)
            if ranges:
                if attempt > 1:
                    print(f"  LLM断句{label}第 {attempt} 次成功")
                return ranges
            errors.append("返回空 segments")
        except Exception as e:  # noqa: BLE001
            errors.append(_short_error(e))
        if attempt < _LLM_SEGMENT_RETRIES:
            print(f"  [warn] LLM断句{label}第 {attempt}/{_LLM_SEGMENT_RETRIES} 次失败：{errors[-1]}，重试")
            time.sleep(0.8 * attempt)
    print(f"  [warn] LLM断句{label}最终失败：{errors[-1] if errors else '未知错误'}")
    return []


def _llm_segment_ranges_once(
    words: list[tuple[float, float, str]],
    llm,
    *,
    max_tokens: int,
) -> list[tuple[int, int]]:
    if not words:
        return []
    base = words[0][0]
    tokens = [
        {
            "i": i,
            "s": round(s - base, 3),
            "e": round(e - base, 3),
            "t": t,
        }
        for i, (s, e, t) in enumerate(words)
    ]
    payload = json.dumps({"tokens": tokens}, ensure_ascii=False)
    data = llm.complete_json(_LLM_SEGMENT_SYSTEM, payload, max_tokens=max_tokens)
    raw_segments = data.get("segments", []) if isinstance(data, dict) else []
    return _coerce_llm_segment_ranges(raw_segments, len(words))


def _llm_segment_ranges_chunked(
    words: list[tuple[float, float, str]],
    llm,
    *,
    offset: int = 0,
) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for first, last in _chunk_word_ranges(words):
        local = words[first:last + 1]
        ranges = _llm_segment_ranges_with_retry(
            local,
            llm,
            max_tokens=4096,
            label=f"分块 {offset + first}-{offset + last}",
        )
        if not ranges:
            # 分块失败不再递归拆半（曾导致几十次慢调用的「重试风暴」，单次切片耗时 20+ 分钟）；
            # 直接放弃 LLM 断句，让上层退回快速的停顿断句。
            print(f"  [warn] LLM断句分块 {offset + first}-{offset + last} 失败，放弃 LLM 断句改用停顿断句")
            return []
        out.extend((a + first, b + first) for a, b in ranges)
    return out


def _chunk_word_ranges(
    words: list[tuple[float, float, str]],
    *,
    max_words: int = _LLM_SEGMENT_CHUNK_WORDS,
    lookaround: int = 24,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    n = len(words)
    while start < n:
        if n - start <= max_words:
            ranges.append((start, n - 1))
            break
        target = start + max_words
        lo = max(start + _LLM_SEGMENT_MIN_CHUNK_WORDS, target - lookaround)
        hi = min(n - 1, target + lookaround)
        best = target
        best_gap = -1.0
        for i in range(lo, hi + 1):
            gap = words[i][0] - words[i - 1][1]
            if gap > best_gap:
                best_gap = gap
                best = i
        ranges.append((start, best - 1))
        start = best
    return ranges


def _short_error(err: Exception, limit: int = 180) -> str:
    text = str(err).replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _coerce_llm_segment_ranges(raw_segments: object, n_words: int) -> list[tuple[int, int]]:
    if not isinstance(raw_segments, list) or n_words <= 0:
        return []
    ranges: list[tuple[int, int]] = []
    prev_last = -1
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        first = _coerce_index(item, "start_i", "first_i", "start", "from")
        last = _coerce_index(item, "end_i", "last_i", "end", "to")
        if first is None or last is None:
            continue
        first = max(0, min(n_words - 1, first))
        last = max(0, min(n_words - 1, last))
        if last < first:
            continue
        if first <= prev_last:
            first = prev_last + 1
        if first >= n_words:
            break
        last = max(first, last)
        ranges.append((first, last))
        prev_last = last
    return ranges


def _coerce_index(item: dict, *keys: str) -> int | None:
    for key in keys:
        if key not in item:
            continue
        try:
            return int(item[key])
        except (TypeError, ValueError):
            continue
    return None


def _fill_missing_token_ranges(ranges: list[tuple[int, int]], n_words: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cursor = 0
    for first, last in ranges:
        if first > cursor:
            out.extend(_fallback_token_ranges(cursor, first - 1))
        out.append((first, last))
        cursor = last + 1
    if cursor < n_words:
        out.extend(_fallback_token_ranges(cursor, n_words - 1))
    return out


def _fallback_token_ranges(first: int, last: int, *, max_tokens: int = 8) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur = first
    while cur <= last:
        end = min(last, cur + max_tokens - 1)
        out.append((cur, end))
        cur = end + 1
    return out


_SMALL_HIRAGANA = "ぁぃぅぇぉゃゅょゎっ"
_SMALL_KATAKANA = "ァィゥェォャュョヮッ"
_KANA_CONTINUATION = _SMALL_HIRAGANA + _SMALL_KATAKANA + "ー〜"
_KATAKANA_CHARS = "ァ-ヴー〜"


def _repair_japanese_word_splits(cues: list[Cue], *, max_gap: float = 0.25) -> list[Cue]:
    """Fix conservative ASR cue splits inside a Japanese word.

    Parakeet/Kotoba output can be token-like. Our short-cue splitter may flush at
    the 24-char/6-sec boundary and leave fragments such as ``チ`` / ``ェキ`` or
    ``リリー`` / ``ベ`` in adjacent subtitles. This pass only moves obvious
    continuation prefixes back to the previous cue; it avoids broadly merging
    normal short sentences.
    """
    if len(cues) < 2:
        return cues
    out: list[Cue] = []
    i = 0
    while i < len(cues):
        cur = cues[i]
        if i + 1 >= len(cues):
            out.append(cur)
            break
        nxt = cues[i + 1]
        gap = max(0.0, nxt.start - cur.end)
        prefix = _leading_word_split_prefix(cur.ja, nxt.ja) if gap <= max_gap else ""
        if not prefix:
            out.append(cur)
            i += 1
            continue

        rest = nxt.ja[len(prefix):].lstrip()
        moved_end = _prefix_end_time(nxt, prefix)
        cur = Cue(cur.start, max(cur.end, moved_end), cur.ja + prefix, cur.zh)
        if rest:
            next_start = max(cur.end + 0.02, moved_end)
            if nxt.end - next_start < 0.12:
                out.append(Cue(cur.start, max(cur.end, nxt.end),
                               cur.ja + rest, _join_zh(cur.zh, nxt.zh)))
                i += 2
                continue
            nxt = Cue(next_start, nxt.end, rest, nxt.zh)
            out.append(cur)
            cues[i + 1] = nxt
            i += 1
        else:
            out.append(Cue(cur.start, max(cur.end, nxt.end), cur.ja, _join_zh(cur.zh, nxt.zh)))
            i += 2
    return out


def _leading_word_split_prefix(prev: str, nxt: str) -> str:
    prev = (prev or "").strip()
    nxt = (nxt or "").strip()
    if not prev or not nxt:
        return ""
    if prev[-1] in "。.!！？?、，,「『（(":
        return ""
    if nxt[0] in _SMALL_KATAKANA or nxt[0] in "ー〜":
        return _leading_katakana_run(nxt) or nxt[0]
    if nxt[0] in _SMALL_HIRAGANA:
        return _leading_small_hiragana_phrase(nxt)
    if re.search(f"[{_KATAKANA_CHARS}]$", prev) and re.search(f"^[{_KATAKANA_CHARS}]", nxt):
        return _leading_katakana_run(nxt)
    return ""


def _leading_katakana_run(text: str) -> str:
    m = re.match(f"^[{_KATAKANA_CHARS}{_SMALL_KATAKANA}]+", text or "")
    return m.group(0) if m else ""


def _leading_small_hiragana_phrase(text: str) -> str:
    for phrase in ("っていう", "ったり", "っち", "って", "った", "っぽい"):
        if text.startswith(phrase):
            return phrase
    return text[:2] if len(text) >= 2 else text


def _prefix_end_time(cue: Cue, prefix: str) -> float:
    text_len = max(1, len(cue.ja or ""))
    ratio = min(1.0, max(0.05, len(prefix) / text_len))
    dur = max(0.12, min(0.8, (cue.end - cue.start) * ratio))
    return min(cue.end, cue.start + dur)


def _join_zh(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if a and b and a != b:
        return f"{a} {b}"
    return a or b


def _span_asr_cues(segments: list[dict], span: SongSpan, origin: float) -> list[Cue]:
    out: list[Cue] = []
    for seg in segments:
        st, en = float(seg.get("start", 0)), float(seg.get("end", 0))
        if not _overlaps(st, en, span.start, span.end):
            continue
        out.append(Cue(
            start=max(st, span.start) - origin,
            end=min(en, span.end) - origin,
            ja=(seg.get("ja") or "").strip(),
            zh=(seg.get("zh") or "").strip(),
        ))
    return out


def _anchor_cues_in_span(anchor_cues: list[Cue], span: SongSpan, origin: float) -> list[Cue]:
    rel_start = span.start - origin
    rel_end = span.end - origin
    return [
        c for c in anchor_cues
        if _overlaps(c.start, c.end, rel_start, rel_end)
    ]


def _retime_cues_with_anchors(cues: list[Cue], anchors: list[Cue]) -> list[Cue]:
    if not cues or not anchors:
        return cues
    out: list[Cue] = []
    used: set[int] = set()
    cursor = 0
    for cue in sorted(cues, key=lambda c: c.start):
        best_i, best_score = _best_anchor_index(cue.ja, anchors, cursor, used)
        if best_i >= 0 and best_score >= clip_config.lyrics_match_min_score:
            anchor = anchors[best_i]
            out.append(Cue(anchor.start, max(anchor.end, anchor.start + 0.5), cue.ja, cue.zh))
            used.add(best_i)
            cursor = best_i + 1
        else:
            out.append(cue)
    return out


def _best_anchor_index(text: str, anchors: list[Cue], cursor: int,
                       used: set[int]) -> tuple[int, float]:
    lo = max(0, cursor - 3)
    hi = min(len(anchors), cursor + 18)
    best_i, best_score = -1, 0.0
    for i in range(lo, hi):
        if i in used:
            continue
        score = _similarity(text, anchors[i].ja)
        if score > best_score:
            best_i, best_score = i, score
    return best_i, best_score


def _netease_span_cues(
    span: SongSpan,
    asr_cues: list[Cue],
    clip_start: float,
    clip_end: float,
    origin: float,
    llm=None,
) -> list[Cue]:
    bundle = fetch_authorized_lyrics_for_candidates([span.title], artist=span.artist)
    if not bundle:
        bundle = fetch_authorized_lyrics_for_candidates(
            _llm_search_candidates_from_asr(span, asr_cues, llm=llm),
            artist=span.artist,
        )
    if not bundle:
        return []
    return _direct_official_span_cues(span, bundle.lines, origin)


def _direct_official_span_cues(span: SongSpan, lines: list[LyricsLine], origin: float) -> list[Cue]:
    """铺整首授权歌词到 song span。

    歌唱 ASR 常会空白或幻觉，所以这里不使用 ASR 文本。以第一条可展示歌词作为
    span.start 的锚点，并按整段演唱时长轻微伸缩官方时间轴。
    """
    if not lines or span.end <= span.start:
        return []
    first = lines[0].start
    official_dur = max(lines[-1].end - first, 1.0)
    span_dur = span.end - span.start
    scale = span_dur / official_dur
    if not (0.75 <= scale <= 1.35):
        scale = 1.0

    out: list[Cue] = []
    for line in lines:
        start = span.start + (line.start - first) * scale
        end = span.start + (line.end - first) * scale
        start = max(span.start, start)
        end = min(span.end, max(end, start + 0.4))
        if end <= span.start or start >= span.end or end <= start:
            continue
        out.append(Cue(start - origin, end - origin, line.ja, line.zh))
    return out


def _llm_search_candidates_from_asr(span: SongSpan, asr_cues: list[Cue], llm=None) -> list[str]:
    if not asr_cues:
        return []
    snippets = _asr_snippets_for_search(asr_cues)
    if not snippets:
        return []
    try:
        from src.llm.client import LLMClient
        client = llm or LLMClient()
        data = client.complete_json(
            "你只负责把噪声 ASR 中的歌唱片段归纳成歌曲检索查询。"
            "请结合已知曲名/artist 和可能错字、重复、幻觉的歌词片段，推断最可能的曲名与原唱。"
            "不要输出歌词正文，不要补全歌词。严格输出 JSON："
            '{"queries":["曲名 artist", "曲名"]}。最多 5 个查询。',
            f"已知曲名：{span.title}\n已知 artist：{span.artist or ''}\n"
            f"噪声 ASR 片段（可能错字/重复/幻觉）：\n{snippets}",
            max_tokens=512,
        )
    except Exception:
        return []
    queries = data.get("queries", []) if isinstance(data, dict) else []
    out: list[str] = []
    for q in queries:
        q = str(q or "").strip()
        if q and q not in out:
            out.append(q)
    return out


def _asr_snippets_for_search(asr_cues: list[Cue], max_lines: int = 12, max_chars: int = 700) -> str:
    lines: list[str] = []
    total = 0
    for cue in asr_cues:
        text = (cue.ja or "").strip()
        if not text:
            continue
        text = re.sub(r"\s+", " ", text)
        if len(text) > 80:
            text = text[:77] + "..."
        total += len(text) + 1
        if total > max_chars or len(lines) >= max_lines:
            break
        lines.append(text)
    return "\n".join(lines)


def _estimate_lyric_offset(
    lines: list[LyricsLine],
    asr_cues: list[Cue],
    *,
    default_offset: float,
) -> float:
    """用 ASR 中能匹配到的歌词行估计官方歌词整体偏移。

    这样 SongSpan 即使把介绍/感想也包进来，官方歌词仍会从实际开唱位置附近开始。
    """
    matches: list[tuple[float, float, float, int]] = []
    for asr in asr_cues:
        if not asr.ja:
            continue
        best_i, best_score = _best_line_index(asr.ja, lines, 0)
        if best_i >= 0 and best_score >= clip_config.lyrics_match_min_score:
            matches.append((best_score, asr.start - lines[best_i].start, asr.start, best_i))
    if not matches:
        return max(0.0, default_offset)
    early_matches = [
        m for m in sorted(matches, key=lambda x: x[2])
        if m[3] <= 3 and m[0] >= max(0.55, clip_config.lyrics_match_min_score)
    ]
    if early_matches:
        return max(0.0, early_matches[0][1])
    matches.sort(key=lambda x: x[0], reverse=True)
    offsets = sorted(offset for _, offset, _, _ in matches[:5])
    mid = len(offsets) // 2
    if len(offsets) % 2:
        offset = offsets[mid]
    else:
        offset = (offsets[mid - 1] + offsets[mid]) / 2
    return max(0.0, offset)


def _edge_asr_cues_outside_official(
    asr_cues: list[Cue],
    official: list[Cue],
    lines: list[LyricsLine],
) -> list[Cue]:
    """保留官方歌词覆盖范围前后的 ASR 谈话。

    SongSpan 可能为了切片完整性包含报曲、作品说明和唱后感想；这些句子没有官方歌词
    对应行。官方歌词整体偏移后，若直接只返回 official，会在开唱前/唱完后留下无字幕
    空洞，所以边缘处不匹配歌词的 ASR cue 要补回。
    """
    if not official:
        return []
    first = min(c.start for c in official)
    last = max(c.end for c in official)
    out: list[Cue] = []
    for cue in asr_cues:
        if not cue.ja:
            continue
        outside = cue.end <= first - 0.05 or cue.start >= last + 0.05
        if not outside:
            continue
        _, score = _best_line_index(cue.ja, lines, 0)
        if score >= clip_config.lyrics_match_min_score:
            continue
        out.append(cue)
    return out


def _retime_official_with_asr(official: list[Cue], asr_cues: list[Cue]) -> list[Cue]:
    used: set[int] = set()
    for asr in asr_cues:
        best_i, best_score = -1, 0.0
        for i, cue in enumerate(official):
            if i in used:
                continue
            if not _nearby_for_retime(asr, cue):
                continue
            score = _similarity(asr.ja, cue.ja)
            if score > best_score:
                best_i, best_score = i, score
        if best_i < 0 or best_score < clip_config.lyrics_match_min_score:
            continue
        target = official[best_i]
        if best_score < clip_config.lyrics_official_min_score:
            target.ja = asr.ja
            target.zh = target.zh or asr.zh
        target.start = asr.start
        target.end = max(asr.end, asr.start + 0.5)
        used.add(best_i)
    official.sort(key=lambda c: c.start)
    return official


def _nearby_for_retime(asr: Cue, cue: Cue) -> bool:
    """Only use ASR as a local timing hint; repeated lyrics can match far away."""
    if _overlaps(asr.start, asr.end, cue.start, cue.end):
        return True
    return min(abs(asr.start - cue.start), abs(asr.end - cue.end)) <= 8.0
