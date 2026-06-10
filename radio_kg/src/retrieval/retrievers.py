"""The two GraphRAG retrieval branches (PRD 4.1).

Each branch yields `Passage` items carrying the text used for fusion plus the
provenance citation, so the answer can cite [episode + timestamp].
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from src.mcp_layer.graph_store import GraphStore
from src.mcp_layer.vector_store import VectorStore


@dataclass
class Passage:
    key: str                 # dedup key
    text: str                # content shown to the LLM
    citation: str
    origin: str              # "vector" | "graph"
    score: float = 0.0       # branch-local score (distance or relevance)
    meta: dict = field(default_factory=dict)


class VectorRetriever:
    """Semantic recall of dialogue chunks — fuzzy / mood / topic questions."""

    def __init__(self, vector: VectorStore):
        self.vector = vector

    def retrieve(self, question: str | list[str], k: int = 6) -> list[Passage]:
        queries = self._queries(question)
        out: list[Passage] = []
        by_key: dict[str, Passage] = {}

        def add(p: Passage, query_rank: int):
            p.score += query_rank * 0.05
            if p.key in by_key:
                by_key[p.key].score = min(by_key[p.key].score, p.score)
                return
            out.append(p)
            by_key[p.key] = p

        for query_rank, query in enumerate(queries):
            for p in self._to_passages(self.vector.query(query, n_results=k)):
                add(p, query_rank)
        for query_rank, query in enumerate(queries):
            for p in self._to_passages(self.vector.keyword_query(query, n_results=3)):
                add(p, query_rank)
        return sorted(out, key=lambda p: p.score)

    @staticmethod
    def _queries(question: str | list[str]) -> list[str]:
        if isinstance(question, str):
            raw = [question]
        else:
            raw = question
        queries = []
        for q in raw:
            q = " ".join(str(q or "").split())
            if q and q not in queries:
                queries.append(q)
        return queries or [""]

    @staticmethod
    def _to_passages(res: dict) -> list[Passage]:
        if not res or not res.get("ids"):
            return []
        ids = res["ids"][0]
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        dists = res.get("distances", [[0.0] * len(ids)])[0]
        out = []
        for cid, doc, meta, dist in zip(ids, docs, metas, dists):
            out.append(
                Passage(
                    key=f"chunk:{cid}",
                    text=doc,
                    citation=meta.get("citation", ""),
                    origin="vector",
                    score=float(dist),
                    meta=meta,
                )
            )
        return out


class GraphRetriever:
    """Multi-hop topological recall — precise cross-episode relational facts."""

    def __init__(self, graph: GraphStore):
        self.graph = graph

    def retrieve(
        self,
        anchor_terms: list[str],
        hops: int = 2,
        limit: int = 40,
        query: str = "",
    ) -> list[Passage]:
        eids: list[str] = []
        seen = set()
        for term in anchor_terms:
            for hit in self.graph.search_nodes(term):
                if hit["eid"] not in seen:
                    seen.add(hit["eid"])
                    eids.append(hit["eid"])
        if not eids:
            return []
        edges = self.graph.neighbors(eids, hops=hops, limit=limit)
        out = []
        terms = self._terms(anchor_terms, query)
        for i, e in enumerate(edges):
            active = "（现行）" if e.get("end_epoch") is None else f"（至第{e['end_epoch']}期止）"
            triple = f"{e['subject']} —[{e['relation']}]→ {e['object']} {active}"
            relevance = sum(1 for term in terms if term in triple)
            out.append(
                Passage(
                    key=f"edge:{e['subject']}|{e['relation']}|{e['object']}",
                    text=triple,
                    citation=e.get("citation", ""),
                    origin="graph",
                    score=float(i) - (relevance * 100.0),
                    meta=e,
                )
            )
        return sorted(out, key=lambda p: p.score)

    @staticmethod
    def _terms(anchor_terms: list[str], query: str) -> list[str]:
        raw = anchor_terms + re.split(r"[\s,，。？?]+", query)
        terms = [term for term in dict.fromkeys(t.strip() for t in raw) if len(term) >= 2]
        joined = " ".join(terms)
        if "所属" in joined or "事務所" in joined or "事务所" in joined:
            terms.extend(["所属", "事務所", "プロダクション"])
        return list(dict.fromkeys(terms))
