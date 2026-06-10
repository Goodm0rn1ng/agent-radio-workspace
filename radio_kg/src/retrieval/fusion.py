"""Fusion layer (PRD 4.1 step 3): cross-fit the vector and graph branches.

Uses Reciprocal Rank Fusion (RRF) — a rank-based, dependency-free reranker that
combines heterogeneous result lists without needing comparable raw scores. A
cross-encoder reranker can be dropped in behind the same `fuse()` signature later.
"""
from __future__ import annotations

import re

from src.retrieval.retrievers import Passage

RRF_K = 60


def _ranked(passages: list[Passage]) -> list[Passage]:
    # vector: lower distance is better; graph: lower rank index is better
    return sorted(passages, key=lambda p: p.score)


def fuse(
    vector_hits: list[Passage],
    graph_hits: list[Passage],
    top_n: int = 10,
) -> list[Passage]:
    scores: dict[str, float] = {}
    best: dict[str, Passage] = {}
    for branch in (vector_hits, graph_hits):
        for rank, p in enumerate(_ranked(branch)):
            scores[p.key] = scores.get(p.key, 0.0) + 1.0 / (RRF_K + rank + 1)
            best.setdefault(p.key, p)
    fused = sorted(best.values(), key=lambda p: scores[p.key], reverse=True)
    return fused[:top_n]


def build_context(passages: list[Passage]) -> str:
    """Render fused passages as a numbered, citation-tagged context block."""
    lines = []
    for i, p in enumerate(passages, 1):
        tag = {
            "graph": "图谱事实",
            "summary": "结构化摘要",
        }.get(p.origin, "对话片段")
        cite = p.citation or "出处不明"
        lines.append(f"[{i}] ({tag}) SOURCE: {cite}\n{p.text}")
    return "\n".join(lines)


_CITATION_RE = re.compile(r"【出[処处]:\s*(?:\[)?(\d+)(?:\])?[^】]*】")


def expand_citation_refs(answer: str, passages: list[Passage]) -> str:
    """Replace model-emitted context-number citations with real source spans."""
    citation_by_index = {
        str(i): p.citation for i, p in enumerate(passages, 1) if p.citation
    }

    def repl(match: re.Match) -> str:
        citation = citation_by_index.get(match.group(1))
        if not citation:
            return match.group(0).replace("出処", "出处")
        return f"【出处:{citation}】"

    return _CITATION_RE.sub(repl, answer)
