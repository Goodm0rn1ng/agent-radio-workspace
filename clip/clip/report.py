"""节目报告：从该期摘要派生「主播在什么时间做了什么」的分板块时间线 + 统计。

确定性地读 05_summary 的小节（已含 time_range / 标题 / 描述），不复刻歌词、不依赖 LLM；
按板块类型聚合时长，给出大致时间戳时间线，供前端「节目报告」展示。
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

# 板块类型分类：显式的「游戏/来信/雑談」标记优先于「演唱」，避免谈话块被误判为演唱。
# 主要看小节标题（节目自带的板块名），描述仅作兜底。
_TYPE_RULES = [
    (("ゲーム", "game", "実況", "游戏", "マイクラ"), "🎮 游戏"),
    (("お便り", "メール", "便り", "来信", "コメント", "コメ", "質問"), "✉️ 来信/互动"),
    (("トーク", "talk", "雑談", "挨拶", "オープニング", "エンディング", "宣言",
      "中二病", "感想", "近況", "フリー"), "💬 雑談/トーク"),
    (("歌枠", "ソング", "song", "メドレー", "アニソン", "カバー", "うた", "ブロック",
      "ラスト", "演唱", "歌回", "曲", "神曲"), "🎵 演唱/歌枠"),
]


def _classify(title: str, intro: str) -> str:
    for kws, label in _TYPE_RULES:        # 先按标题判，命中即返回（显式标记优先）
        if any(k in title for k in kws):
            return label
    blob = f"{title}{intro}"              # 标题没命中再看描述兜底
    for kws, label in _TYPE_RULES:
        if any(k in blob for k in kws):
            return label
    return "・ 其它"


def _sec(t: str) -> float:
    p = [x for x in str(t).strip().split(":") if x != ""]
    try:
        p = [float(x) for x in p]
    except ValueError:
        return 0.0
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    return p[0] if p else 0.0


def _hms(s: float) -> str:
    s = int(s)
    return f"{s//3600:d}:{s%3600//60:02d}:{s%60:02d}" if s >= 3600 else f"{s//60:d}:{s%60:02d}"


def build_report(episode_dir: str | Path) -> dict:
    D = Path(episode_dir)
    sp = D / "05_summary.json"
    if not sp.exists():
        return {"error": f"无摘要：{D.name}"}
    s = json.loads(sp.read_text(encoding="utf-8"))

    timeline = []
    for sec in s.get("sections", []):
        tr = (sec.get("time_range") or "").split("-")
        st, en = (_sec(tr[0]), _sec(tr[1])) if len(tr) == 2 else (0.0, 0.0)
        title = (sec.get("title_ja") or sec.get("title") or "").strip()
        intro = (sec.get("intro") or "").strip()
        timeline.append({
            "start": st, "end": en, "ts": f"{_hms(st)}–{_hms(en)}",
            "title": title, "type": _classify(title, intro),
            "host_action": intro,          # 主播在此时段做了什么（摘要描述）
        })

    total = max((t["end"] for t in timeline), default=0.0)
    by_type: Counter = Counter()
    for t in timeline:
        by_type[t["type"]] += max(t["end"] - t["start"], 0)

    songs = []
    try:
        from clip.setlist import extract_setlist
        from src.llm.client import LLMClient
        songs = extract_setlist(s, LLMClient())
    except Exception:  # noqa: BLE001
        songs = []

    return {
        "episode": D.name,
        "overview": (s.get("summary", "") or "")[:500],
        "duration": total,
        "duration_hms": _hms(total),
        "timeline": timeline,
        "stats": {
            "sections": len(timeline),
            "songs": len(songs),
            "by_type": [{"type": k, "sec": round(v), "hms": _hms(v),
                         "pct": round(v / total * 100) if total else 0}
                        for k, v in by_type.most_common()],
        },
        "songs": [{"title": x.title, "ts": f"{_hms(x.start)}–{_hms(x.end)}",
                   "start": x.start, "end": x.end} for x in songs],
    }
