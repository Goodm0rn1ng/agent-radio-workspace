"""Corpus-wide listener-mail analytics over `persona/mail_exemplars.json`.

The graph's `投稿` edges support a sender ranking but carry no mail *content* or
*section*, and windowed retrieval only ever sees a few episodes — so neither can
answer global mail questions ("top-3 mail themes", "which segment does the
top sender favour"). This agent scans the complete mail-exemplar artifact
(every read-out listener mail, with sender / theme text / section / citation),
so the answers are corpus-complete rather than top-k.

- sender_ranking: deterministic (honorific-normalized count + favourite
  canonical segment + example citations).
- theme_distribution: ONE LLM pass classifies each mail into a theme bucket;
  the COUNTS are then computed deterministically from those labels (no
  LLM-fabricated statistics), with example citations per theme.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

from config.settings import settings
from src.llm.client import LLMClient, LLMError
from src.mcp_layer.graph_store import _strip_honorific

# canonical recurring segments; first keyword hit wins (order = priority)
_SECTION_CANON = [
    ("プチニュース", "週刊・こもりすプチニュース"),
    ("プチnews", "週刊・こもりすプチニュース"),
    ("褒められ", "褒められたい!応援されたい!"),
    ("応援され", "褒められたい!応援されたい!"),
    ("頬袋", "こもりすの頬袋"),
    ("未来にあったら", "未来にあったらいいもの"),
    ("宝物", "宝物のお便り"),
    ("こもれ話", "こもれ話"),
    ("木漏话", "こもれ話"),
    ("木漏話", "こもれ話"),
    ("告知", "告知事項"),
    ("オープニング", "オープニング"),
    ("エンディング", "エンディング"),
    ("お便り", "お便り紹介"),
    ("メール", "お便り紹介"),
    ("来信", "お便り紹介"),
    ("来函", "お便り紹介"),
]

# seed theme taxonomy for mail classification (the LLM may fall back to 其它)
_THEMES = [
    "恋爱/感情咨询", "职场/工作烦恼", "学业/考试/升学", "人际关系", "推活/追星",
    "日常生活/小确幸", "节目感想/祝贺", "圣地巡礼/旅行", "自我成长/迷茫",
    "美食", "季节/天气", "创意/趣味分享", "回忆/宝物", "健康/睡眠",
]

_CLASSIFY_SYSTEM = (
    "你在为一档电台节目的听众来信分类。下面给出编号的来信摘要，请把【每一封】来信归入"
    "语义最贴切的一个主题（尽量用下列固定主题，实在不合适才用『其它』）：\n"
    + "、".join(_THEMES) + "。\n"
    '只输出 JSON：{"assign":{"0":"主题","1":"主题", ...}}，键为来信编号字符串，覆盖所有给定编号。'
)


def _canon_section(title: str) -> str:
    t = title or ""
    for kw, canon in _SECTION_CANON:
        if kw in t:
            return canon
    return "お便り紹介"


class MailAnalytics:
    def __init__(self, llm: LLMClient, path: Path | None = None):
        self.llm = llm
        self.path = path or (settings.abspath("persona") / "mail_exemplars.json")
        self._records = self._load()
        self._theme_cache: dict[int, dict] | None = None

    def _load(self) -> list[dict]:
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in data if (r.get("mail_from") or "").strip()]

    @property
    def available(self) -> bool:
        return bool(self._records)

    # ── Q6 / Q7: who wrote the most mail, and to which segment ─────────
    def sender_ranking(self, n: int = 10) -> dict:
        counts: collections.Counter = collections.Counter()
        sections: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        display: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        cites: dict[str, list[str]] = collections.defaultdict(list)
        for r in self._records:
            raw = (r.get("mail_from") or "").strip()
            key = _strip_honorific(raw)
            counts[key] += 1
            sections[key][_canon_section(r.get("title", ""))] += 1
            display[key][raw] += 1
            cit = r.get("citation")
            if cit and len(cites[key]) < 5:
                cites[key].append(cit)
        rows = []
        for key, cnt in counts.most_common(n):
            name = display[key].most_common(1)[0][0]
            fav = sections[key].most_common(1)[0]
            rows.append({"name": name, "count": cnt,
                         "fav_section": fav[0], "fav_section_count": fav[1],
                         "citations": cites[key]})
        return {"rows": rows, "total_senders": len(counts),
                "total_mail": sum(counts.values())}

    def sender_ranking_answer(self, n: int = 5) -> dict:
        d = self.sender_ranking(n)
        if not d["rows"]:
            return {"answer": "", "fallback": True}
        top = d["rows"][0]
        lines = [
            f"在所有被读到的听众来信中，来信次数最多的是「{top['name']}」，"
            f"共 {top['count']} 次，最常投稿到「{top['fav_section']}」环节"
            f"（该环节 {top['fav_section_count']} 次）。",
            "",
            f"来信次数排行（共 {d['total_senders']} 位投稿者、{d['total_mail']} 封被读到的来信）：",
        ]
        sources = []
        for i, r in enumerate(d["rows"], 1):
            cite = r["citations"][0] if r["citations"] else ""
            tail = f"【出处:{cite}】" if cite else ""
            lines.append(f"{i}. {r['name']}（{r['count']}次，多投「{r['fav_section']}」）{tail}")
            for c in r["citations"]:
                sources.append({"text": f"{r['name']} 投稿", "citation": c, "origin": "mail"})
        return {"answer": "\n".join(lines), "sources": sources, "result": d["rows"]}

    # ── Q5: top mail themes ────────────────────────────────────────────
    def _classify(self, batch: int = 50) -> dict[int, dict]:
        """One classification per mail. Batched so the JSON reply never exceeds
        the model's output-token limit (a single 184-key reply gets truncated)."""
        if self._theme_cache is not None:
            return self._theme_cache
        assign: dict[str, str] = {}
        for base in range(0, len(self._records), batch):
            chunk = self._records[base:base + batch]
            items = []
            for j, r in enumerate(chunk):
                txt = " ".join(str(r.get("mail", "")).split())[:90]
                items.append(f"{base + j}. [{r.get('mail_from','')}] {txt}")
            user = "来信列表：\n" + "\n".join(items)
            for _ in range(2):
                try:
                    d = self.llm.complete_json(_CLASSIFY_SYSTEM, user, max_tokens=4096)
                    part = d.get("assign") or {}
                    if part:
                        assign.update({str(k): v for k, v in part.items()})
                        break
                except LLMError:
                    pass
        out: dict[int, dict] = {}
        for i, r in enumerate(self._records):
            theme = assign.get(str(i)) or "其它"
            out[i] = {"theme": theme, "rec": r}
        self._theme_cache = out
        return out

    def theme_distribution(self, n: int = 3) -> dict:
        labeled = self._classify()
        counts: collections.Counter = collections.Counter()
        examples: dict[str, list[dict]] = collections.defaultdict(list)
        for v in labeled.values():
            theme = v["theme"]
            counts[theme] += 1
            r = v["rec"]
            if len(examples[theme]) < 3 and r.get("citation"):
                examples[theme].append({"from": r.get("mail_from", ""),
                                        "mail": str(r.get("mail", ""))[:60],
                                        "citation": r.get("citation", "")})
        total = sum(counts.values()) or 1
        rows = []
        for theme, cnt in counts.most_common():
            if theme == "其它":
                continue
            rows.append({"theme": theme, "count": cnt,
                         "pct": round(100 * cnt / total),
                         "examples": examples[theme]})
        return {"rows": rows[:max(n, 3)], "total": total, "top_n": n}

    def theme_distribution_answer(self, n: int = 3) -> dict:
        d = self.theme_distribution(n)
        if not d["rows"]:
            return {"answer": "", "fallback": True}
        lines = [f"在全部 {d['total']} 封被读到的听众来信中，最常被探讨的主题排名如下："]
        sources = []
        for i, r in enumerate(d["rows"][:n], 1):
            ex = r["examples"][0] if r["examples"] else None
            tail = f"（例：{ex['from']}「{ex['mail']}」【出处:{ex['citation']}】）" if ex else ""
            lines.append(f"{i}. {r['theme']}：约 {r['count']} 封（{r['pct']}%）{tail}")
            for e in r["examples"]:
                sources.append({"text": f"{e['from']}：{e['mail']}",
                                "citation": e["citation"], "origin": "mail"})
        return {"answer": "\n".join(lines), "sources": sources, "result": d["rows"]}
