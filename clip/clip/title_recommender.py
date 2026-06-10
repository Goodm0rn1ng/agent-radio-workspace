"""Manual slice title recommendation from current trend signals."""
from __future__ import annotations

import json
import re
from typing import Any


_TITLE_SYSTEM = """你是 B 站 VTuber/ACGN 切片标题策划。给你一段新切片的字幕内容，
以及一个「最近可能火爆的因素」和一个「涨播放最快的视频」作为参考。

请给这个切片推荐标题。要求：
- 标题必须忠于切片内容，不能把参考视频里没有出现在切片中的事实硬套进去。
- 可以借鉴参考视频的标题节奏、热词方向或观众兴趣，但不要碰瓷、不要夸大。
- 如果切片是歌唱区间，突出歌曲名、原唱/企划/主播反应；如果是谈话区间，突出最有传播性的反差或梗。
- 标题适合 B 站，简体中文为主，可保留必要日文专名。
- 推荐标题不超过 32 个中文字符。

严格输出 JSON：
{"recommended_title":"推荐标题","alternatives":["备选1","备选2"],"reason":"一句话说明为什么这样命名"}"""


def recommend_title(cues: list[dict], trend_data: dict[str, Any], llm=None,
                    *, performer: str = "", program: str = "") -> dict[str, Any]:
    factor = _first_factor(trend_data)
    video = (trend_data.get("videos") or [{}])[0] if isinstance(trend_data, dict) else {}
    fallback = _fallback_title(cues, factor, video, performer=performer)
    if llm is None:
        return fallback
    payload = {
        "performer": performer,
        "program": program,
        "clip_subtitles": _clip_lines(cues),
        "hot_factor": factor,
        "fastest_video": {
            "title": video.get("title", ""),
            "owner": video.get("owner", ""),
            "partition": video.get("partition", ""),
            "momentum": video.get("momentum", 0),
            "view": video.get("view", 0),
            "hours": video.get("hours", 0),
        },
    }
    try:
        data = llm.complete_json(_TITLE_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=1200)
    except Exception as e:  # noqa: BLE001
        fallback["reason"] = f"{fallback['reason']}（LLM 推荐失败：{e}）"
        return fallback
    title = _clean_title(data.get("recommended_title") or fallback["recommended_title"])
    if _looks_placeholder(title):
        title = fallback["recommended_title"]
    alternatives = [
        _clean_title(x) for x in (data.get("alternatives") or [])
        if _clean_title(x)
    ][:2]
    return {
        "recommended_title": title,
        "alternatives": alternatives or fallback["alternatives"],
        "reason": (data.get("reason") or fallback["reason"]).strip(),
        "factor": factor,
        "reference_video": payload["fastest_video"],
        "suggested_filename": _filename(title),
    }


def _first_factor(trend_data: dict[str, Any]) -> dict[str, Any]:
    factors = trend_data.get("factors") or [] if isinstance(trend_data, dict) else []
    for f in factors:
        topic = str(f.get("topic") or "")
        if topic and not topic.startswith("(爆火因素分析失败"):
            return {
                "topic": topic,
                "keywords": f.get("keywords") or [],
                "hot_songs": f.get("hot_songs") or [],
                "hook": f.get("hook") or "",
            }
    return {}


def _clip_lines(cues: list[dict], max_chars: int = 1800) -> str:
    lines: list[str] = []
    total = 0
    for c in cues:
        ja = str(c.get("ja") or "").strip()
        zh = str(c.get("zh") or "").strip()
        if not ja and not zh:
            continue
        line = f"[{float(c.get('start') or 0):.1f}-{float(c.get('end') or 0):.1f}] {ja}"
        if zh:
            line += f" / {zh}"
        total += len(line) + 1
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines)


def _fallback_title(cues: list[dict], factor: dict[str, Any], video: dict[str, Any],
                    *, performer: str = "") -> dict[str, Any]:
    name = performer or "主播"
    song = next((c for c in cues if str(c.get("ja") or "").strip().startswith("♪")), None)
    if song:
        title = str(song.get("ja") or "").strip(" ♪")
        rec = f"{name}翻唱《{title}》"
    else:
        first = next((str(c.get("zh") or c.get("ja") or "").strip() for c in cues if c.get("zh") or c.get("ja")), "")
        rec = first[:28] or f"{name}直播切片"
    return {
        "recommended_title": _clean_title(rec),
        "alternatives": [],
        "reason": "基于切片内容生成的保守标题",
        "factor": factor,
        "reference_video": {
            "title": video.get("title", ""),
            "owner": video.get("owner", ""),
            "partition": video.get("partition", ""),
            "momentum": video.get("momentum", 0),
            "view": video.get("view", 0),
            "hours": video.get("hours", 0),
        },
        "suggested_filename": _filename(rec),
    }


def _clean_title(text: str) -> str:
    title = re.sub(r"\s+", " ", str(text or "")).strip(" -_｜|")
    return title[:48] or "直播切片"


def _looks_placeholder(text: str) -> bool:
    return bool(re.search(r"(?i)(xxx|待定|未知|某某|タイトル|title)", text or ""))


def _filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\n\r\t]+', "_", _clean_title(title))
    name = re.sub(r"\s+", " ", name).strip(" ._")
    return (name or "clip")[:80] + ".mp4"
