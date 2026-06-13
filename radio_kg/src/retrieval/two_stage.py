"""Two-stage coarse-to-fine retrieval (hierarchical / parent-document).

Stage 1 (coarse, cheap): match the question against the Summary DB; read each
hit's [episode + time_range] metadata as clues — NO extra LLM call.
Stage 2 (fine, precise): pull the exact dialogue-chunk window for each clue from
the chunk store, plus an entity-anchored Neo4j subgraph; RRF-fuse and hand only
that condensed context to the LLM.

Mitigations baked in:
- Lost-in-the-Middle: route on dense summaries, then fetch a focused window.
- Token explosion: fetch only clue windows, never whole episodes.
- Summary recall ceiling: if the best summary match is weak (distance above
  `fallback_threshold`), fall back to direct dialogue-chunk retrieval so
  long-tail facts are not lost.
- Graph-answerable questions: the entity-anchored graph branch always runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from config.settings import settings
from src.mcp_layer.vector_store import VectorStore
from src.retrieval.fusion import fuse
from src.retrieval.retrievers import GraphRetriever, Passage, VectorRetriever


@dataclass
class RetrievalDebug:
    path: str = "two_stage"           # two_stage | fallback_direct
    summary_clues: list = field(default_factory=list)
    best_summary_distance: float | None = None
    n_window: int = 0
    n_graph: int = 0
    n_direct: int = 0                 # always-on direct-chunk safety net
    n_fallback: int = 0


class SummaryRetriever:
    """Stage 1: match summaries, return [episode+time_range] clues (metadata)."""

    def __init__(self, summary_store: VectorStore):
        self.store = summary_store

    def route(self, query: str | list[str], k: int = 3) -> tuple[list[dict], float | None]:
        queries = self._queries(query)
        filters = self._filters(queries)
        by_key: dict[tuple[int, str, float, float], dict] = {}
        for query_rank, q in enumerate(queries):
            for res in (
                self.store.query(q, n_results=k),
                self.store.keyword_query(q, n_results=k),
            ):
                if not res or not res.get("ids") or not res["ids"][0]:
                    continue
                docs = res.get("documents", [[]])[0]
                metas = res["metadatas"][0]
                dists = res.get("distances", [[None] * len(metas)])[0]
                for doc, meta, dist in zip(docs, metas, dists):
                    if not self._matches_filters(meta, filters):
                        continue
                    distance = None if dist is None else float(dist) + query_rank * 0.05
                    key = (
                        int(meta["episode"]),
                        str(meta.get("episode_label", "")),
                        float(meta.get("start_sec", 0)),
                        float(meta.get("end_sec", 0)),
                    )
                    clue = {
                        "episode": key[0],
                        "episode_label": key[1],
                        "start_sec": key[2],
                        "end_sec": key[3],
                        "citation": meta.get("citation", ""),
                        "title": meta.get("section_title", ""),
                        "text": doc,
                        "distance": distance,
                    }
                    old = by_key.get(key)
                    if old is None or (
                        clue["distance"] is not None
                        and (old["distance"] is None or clue["distance"] < old["distance"])
                    ):
                        by_key[key] = clue
        # When the user pins a specific episode (e.g. "#105 讲了什么"), the
        # top-K semantic neighbors above often capture only one section of that
        # episode because the global K is shared with other episodes. Supplement
        # with EVERY section of the pinned episodes so coverage questions get
        # the full 7-or-so sections in time order, not a single opening blurb.
        if filters.get("episodes"):
            self._add_all_episode_sections(by_key, filters)
        if self._is_mail_query(queries) and (filters.get("episodes") or filters.get("labels")):
            self._add_filtered_mail_summaries(by_key, filters)
        # primary: semantic hits (distance != None) before pinned-by-filter hits;
        # secondary: distance asc among semantic, start_sec asc among pinned.
        clues = sorted(
            by_key.values(),
            key=lambda c: (
                c["distance"] is None,
                c["distance"] if c["distance"] is not None else c["start_sec"],
            ),
        )[:k]
        if not clues:
            return [], None
        best = min((c["distance"] for c in clues if c["distance"] is not None),
                   default=None)
        return clues, best

    def _add_all_episode_sections(
        self,
        by_key: dict[tuple[int, str, float, float], dict],
        filters: dict,
    ) -> None:
        """Supplement `by_key` with every summary section whose episode is in
        `filters['episodes']`. Honors label filters too. Sections added this way
        carry no semantic distance (sorted to the end with start_sec ordering).
        """
        eps = filters.get("episodes") or set()
        if not eps:
            return
        docs = self.store._get_all_documents()
        documents = docs.get("documents", [])
        metas = docs.get("metadatas", [])
        for doc, meta in zip(documents, metas):
            try:
                ep = int(meta.get("episode") or -1)
            except (TypeError, ValueError):
                continue
            if ep not in eps:
                continue
            if not self._matches_filters(meta, filters):
                continue
            key = (
                ep,
                str(meta.get("episode_label", "")),
                float(meta.get("start_sec", 0)),
                float(meta.get("end_sec", 0)),
            )
            by_key.setdefault(key, {
                "episode": key[0],
                "episode_label": key[1],
                "start_sec": key[2],
                "end_sec": key[3],
                "citation": meta.get("citation", ""),
                "title": meta.get("section_title", ""),
                "text": doc,
                "distance": None,
            })

    def _add_filtered_mail_summaries(
        self,
        by_key: dict[tuple[int, str, float, float], dict],
        filters: dict,
    ) -> None:
        docs = self.store._get_all_documents()
        documents = docs.get("documents", [])
        metas = docs.get("metadatas", [])
        for rank, (doc, meta) in enumerate(zip(documents, metas)):
            if not self._matches_filters(meta, filters):
                continue
            if "来信：" not in doc and "お便り" not in str(meta.get("section_title", "")):
                continue
            key = (
                int(meta["episode"]),
                str(meta.get("episode_label", "")),
                float(meta.get("start_sec", 0)),
                float(meta.get("end_sec", 0)),
            )
            by_key.setdefault(key, {
                "episode": key[0],
                "episode_label": key[1],
                "start_sec": key[2],
                "end_sec": key[3],
                "citation": meta.get("citation", ""),
                "title": meta.get("section_title", ""),
                "text": doc,
                "distance": -100.0 + rank * 0.001,
            })

    @staticmethod
    def _queries(query: str | list[str]) -> list[str]:
        raw = [query] if isinstance(query, str) else query
        queries = []
        for q in raw:
            q = " ".join(str(q or "").split())
            if q and q not in queries:
                queries.append(q)
        return queries or [""]

    @staticmethod
    def _filters(queries: list[str]) -> dict:
        text = " ".join(queries)
        # Accept every common way listeners pin a specific episode:
        #   #N / # N           hash form
        #   第N期 / 第N回      Chinese / Japanese ordinal with prefix
        #   N期 / N回 / N集 / N话 / N話  bare-number forms
        #   EPN / epN          English shorthand
        # Bare digits alone are intentionally NOT enough — too ambiguous.
        pattern = re.compile(
            r"#\s*(\d+)"
            r"|(?:第\s*)?(\d{1,4})\s*[期回集话話]"
            r"|(?:EP|ep|Ep|エピソード|エピ)\s*\.?\s*(\d+)"
        )
        episodes: set[int] = set()
        for groups in pattern.findall(text):
            for g in groups:
                if g:
                    episodes.add(int(g))
        labels = []
        if "こもればなし" in text:
            labels.append("こもればなし")
        if "アーカイブ" in text or "archive" in text.lower() or "存档" in text:
            labels.append("アーカイブ")
        return {"episodes": episodes, "labels": labels}

    @staticmethod
    def _is_mail_query(queries: list[str]) -> bool:
        text = " ".join(queries)
        return any(term in text for term in (
            "来信", "信件", "投稿", "听众", "聽眾", "来信人", "來信人",
            "お便り", "メール", "ラジオネーム", "こもれびネーム",
        ))

    @staticmethod
    def _matches_filters(meta: dict, filters: dict) -> bool:
        episodes = filters.get("episodes") or set()
        if episodes and int(meta.get("episode") or -1) not in episodes:
            return False
        labels = filters.get("labels") or []
        episode_label = str(meta.get("episode_label") or "")
        if labels and not any(label in episode_label for label in labels):
            return False
        return True


class TwoStageRetriever:
    def __init__(
        self,
        summary_store: VectorStore,
        chunk_store: VectorStore,
        graph,
        fallback_threshold: float = 1.1,
        window_pad: float = 5.0,
        hops: int = 2,
        summary_k: int | None = None,
        direct_k: int | None = None,
        fallback_k: int | None = None,
    ):
        self.summary = SummaryRetriever(summary_store)
        self.chunk_store = chunk_store
        self.vector_fallback = VectorRetriever(chunk_store)
        self.graph_retriever = GraphRetriever(graph)
        self.fallback_threshold = fallback_threshold
        self.window_pad = window_pad
        self.hops = hops
        self.summary_k = summary_k or settings.qa_summary_k
        self.direct_k = direct_k or settings.qa_direct_k
        self.fallback_k = fallback_k or settings.qa_fallback_k

    def retrieve(self, question: str, anchors: list[str], search_query: str | list[str] = "",
                 top_n: int = 14, wide: bool = False) -> tuple[list[Passage], RetrievalDebug]:
        """`wide=True` doubles every recall budget — used for enumeration /
        cross-episode aggregation questions ("哪些/全部/清单/时间轴") where the
        default top-k routing structurally under-recalls, and for the
        corrective retry after an abstained answer."""
        dbg = RetrievalDebug()
        boost = 2 if wide else 1
        summary_k, direct_k, fallback_k = (
            self.summary_k * boost, self.direct_k * boost, self.fallback_k * boost)
        q = SummaryRetriever._queries(search_query or [question])
        filters = SummaryRetriever._filters(q)
        graph_query = " ".join(q)

        # entity-anchored graph branch always runs
        graph_hits = self.graph_retriever.retrieve(
            anchors, hops=self.hops, query=graph_query, limit=40 * boost)
        dbg.n_graph = len(graph_hits)

        # Stage 1: route on summaries
        clues, best = self.summary.route(q, k=summary_k)
        dbg.summary_clues = [
            {"episode": c["episode"], "title": c["title"],
             "episode_label": c.get("episode_label", ""),
             "citation": c["citation"], "distance": c["distance"]} for c in clues
        ]
        dbg.best_summary_distance = best

        dialogue: list[Passage] = []
        seen: set[str] = set()

        def _add(p: Passage):
            if p.key not in seen:
                seen.add(p.key)
                dialogue.append(p)

        weak = best is None or (self.fallback_threshold is not None
                                and best > self.fallback_threshold)
        if clues and not weak:
            for rank, c in enumerate(clues):
                if c.get("text"):
                    _add(Passage(
                        key=(f"summary:{c['episode']}|{c.get('episode_label', '')}|"
                             f"{c['start_sec']}|{c['end_sec']}"),
                        text=c["text"], citation=c.get("citation", ""),
                        origin="summary", score=float(rank) - 0.25, meta=c))
            # Stage 2: fetch the precise dialogue window for each clue
            for c in clues:
                rows = self.chunk_store.get_window(
                    c["episode"], c["start_sec"], c["end_sec"],
                    pad=self.window_pad, episode_label=c.get("episode_label", ""))
                for rank, r in enumerate(rows):
                    meta = r["metadata"]
                    _add(Passage(
                        key=f"chunk:{r.get('id')}",   # same scheme as VectorRetriever
                        text=r["text"], citation=meta.get("citation", ""),
                        origin="vector", score=float(rank), meta=meta))
            dbg.n_window = len(dialogue)
            dbg.path = "two_stage"
            # always-on direct-chunk safety net: summary routing can confidently
            # match the wrong section, so fuse in a few directly-retrieved chunks
            for p in self.vector_fallback.retrieve(q, k=direct_k):
                if SummaryRetriever._matches_filters(p.meta, filters):
                    _add(p)
            dbg.n_direct = len(dialogue) - dbg.n_window
        else:
            # routing too weak -> rely on direct dialogue retrieval
            for p in self.vector_fallback.retrieve(q, k=fallback_k):
                if SummaryRetriever._matches_filters(p.meta, filters):
                    _add(p)
            dbg.n_fallback = len(dialogue)
            dbg.path = "fallback_direct"

        fused = fuse(dialogue, graph_hits, top_n=top_n)
        return fused, dbg
