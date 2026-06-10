"""Semantic layer over the local Chroma vector store.

Stores dialogue chunks with full provenance metadata so the future Q&A agent
can do semantic recall and cite [episode + timestamp].
"""
from __future__ import annotations

import re

from config.settings import settings
from src.embeddings.e5 import E5Embedder
from src.mcp_layer.client import McpStdioClient

_KEYWORD_STOPWORDS = {
    "节目", "番組", "最近", "最近看", "什么", "什麼", "どんな", "なに",
    "見た", "観た", "見る", "ください", "こもれびじかん", "电视", "テレビ",
    "家庭",
}


def _is_collection_missing(exc: BaseException) -> bool:
    """Recognize a stale-collection error across chromadb versions.

    Different chromadb releases raise this as NotFoundError, ValueError, or
    InvalidCollectionException — all carry "does not exist" or
    "collection ... not found" in the message. Match on class name + text to
    stay compatible without a hard import dep on each variant.
    """
    cls = type(exc).__name__
    msg = str(exc).lower()
    if cls in ("NotFoundError", "InvalidCollectionException"):
        return True
    return "does not exist" in msg or "collection not found" in msg or "collection [" in msg and "does not exist" in msg


class VectorStore:
    def __init__(self, collection_name: str | None = None):
        self.collection_name = collection_name or settings.effective_vector_collection
        self.embedding_model = settings.vector_embedding_model
        self._direct_client = None
        self._direct_collection = None
        self._embedder = None
        self._mcp = None
        if self.embedding_model != "default":
            return
        data_dir = str(settings.abspath(settings.chroma_path))
        args = settings.mcp_chroma_args.split() + [
            "--client-type", "persistent", "--data-dir", data_dir,
        ]
        self._mcp = McpStdioClient(command=settings.mcp_chroma_command, args=args)

    def __enter__(self):
        if self._uses_direct_chroma:
            self._ensure_direct_collection()
        else:
            self._mcp.start()
            self._ensure_collection()
        return self

    def __exit__(self, *exc):
        if self._mcp is not None:
            self._mcp.close()

    def ping(self) -> dict:
        """Cheap liveness probe for /api/health."""
        try:
            n = self.count()
            mode = "direct" if self._uses_direct_chroma else "mcp"
            return {"ok": True, "mode": mode,
                    "collection": self.collection_name, "count": n}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "collection": self.collection_name,
                    "error": str(e)[:200]}

    @property
    def _uses_direct_chroma(self) -> bool:
        return self.embedding_model != "default"

    def _ensure_collection(self):
        try:
            self._mcp.call_tool(
                "chroma_get_collection_info",
                {"collection_name": self.collection_name},
            )
        except RuntimeError:
            self._mcp.call_tool(
                "chroma_create_collection",
                {"collection_name": self.collection_name},
            )

    def _ensure_direct_collection(self):
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError(
                "chromadb is required for local E5 vector storage. "
                "Run: cd <Agent workspace root> && uv sync"
            ) from e
        self._direct_client = chromadb.PersistentClient(
            path=str(settings.abspath(settings.chroma_path))
        )
        self._refresh_direct_collection()
        if self._embedder is None:
            self._embedder = E5Embedder(self.embedding_model)

    def _refresh_direct_collection(self):
        """Re-bind the cached collection handle by name.

        The chromadb python client caches each Collection's UUID on the handle
        we got at startup. If anyone (e.g. `build_summary_db.py`) drops and
        recreates the collection underneath us, the cached handle keeps
        pointing at a stale UUID and every query 404s. Calling this re-fetches
        the handle bound to the current UUID — cheap, no embedder reload.
        """
        self._direct_collection = self._direct_client.get_or_create_collection(
            name=self.collection_name,
            metadata={"embedding_model": self.embedding_model},
            embedding_function=None,
        )

    def _safe_direct_call(self, fn):
        """Run a direct-chroma op; if the cached collection UUID is stale,
        refresh once and retry. Other errors propagate."""
        try:
            return fn(self._direct_collection)
        except Exception as e:  # noqa: BLE001
            if not _is_collection_missing(e):
                raise
            self._refresh_direct_collection()
            return fn(self._direct_collection)

    def reset_collection(self):
        """Drop and recreate the active collection."""
        if self._uses_direct_chroma:
            if self._direct_client is None:
                self._ensure_direct_collection()
            try:
                self._direct_client.delete_collection(self.collection_name)
            except Exception:
                pass
            self._direct_collection = self._direct_client.get_or_create_collection(
                name=self.collection_name,
                metadata={"embedding_model": self.embedding_model},
                embedding_function=None,
            )
            return
        try:
            self._mcp.call_tool(
                "chroma_delete_collection",
                {"collection_name": self.collection_name},
            )
        except RuntimeError:
            pass
        self._mcp.call_tool(
            "chroma_create_collection",
            {"collection_name": self.collection_name},
        )

    def add_chunks(self, ids: list[str], documents: list[str], metadatas: list[dict]):
        if not ids:
            return
        if self._uses_direct_chroma:
            embeddings = self._embedder.encode_passages(documents)
            clean_metas = [self._clean(m) for m in metadatas]
            self._safe_direct_call(lambda c: c.upsert(
                ids=ids,
                documents=documents,
                metadatas=clean_metas,
                embeddings=embeddings,
            ))
            return
        self._mcp.call_tool(
            "chroma_add_documents",
            {
                "collection_name": self.collection_name,
                "ids": ids,
                "documents": documents,
                "metadatas": [self._clean(m) for m in metadatas],
            },
        )

    def query(self, text: str, n_results: int = 5) -> list:
        if self._uses_direct_chroma:
            embeddings = self._embedder.encode_queries([text])
            return self._safe_direct_call(lambda c: c.query(
                query_embeddings=embeddings,
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            ))
        return self._mcp.call_tool(
            "chroma_query_documents",
            {
                "collection_name": self.collection_name,
                "query_texts": [text],
                "n_results": n_results,
            },
        )

    def keyword_query(self, text: str, n_results: int = 3) -> list:
        terms = [
            t.strip("，。！？?、,.!「」『』()（）")
            for t in re.split(r"\s+", text.strip())
        ]
        terms = [
            t for t in terms
            if len(t) >= 2 and t not in _KEYWORD_STOPWORDS
        ]
        if not terms:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        docs = self._get_all_documents()
        scored = []
        for cid, doc, meta in zip(docs["ids"], docs["documents"], docs["metadatas"]):
            score = sum(doc.count(term) for term in terms)
            if score:
                scored.append((score, cid, doc, meta))
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = scored[:n_results]
        return {
            "ids": [[cid for _, cid, _, _ in top]],
            "documents": [[doc for _, _, doc, _ in top]],
            "metadatas": [[meta for _, _, _, meta in top]],
            "distances": [[-float(score) for score, _, _, _ in top]],
        }

    def _get_all_documents(self) -> dict:
        if self._uses_direct_chroma:
            return self._safe_direct_call(
                lambda c: c.get(include=["documents", "metadatas"])
            )
        return self._mcp.call_tool(
            "chroma_get_documents",
            {
                "collection_name": self.collection_name,
                "include": ["documents", "metadatas"],
            },
        )

    def get_window(self, episode: int, start_sec: float, end_sec: float,
                   pad: float = 0.0, episode_label: str = "") -> list[dict]:
        """Stage-2 fetch: dialogue chunks of one episode overlapping the time
        window [start-pad, end+pad]. Returns [{text, metadata}], time-ordered."""
        lo, hi = start_sec - pad, end_sec + pad
        clauses = [
            {"episode": {"$eq": episode}},
            {"start_time": {"$lte": hi}},
            {"end_time": {"$gte": lo}},
        ]
        if episode_label:
            clauses.append({"episode_label": {"$eq": episode_label}})
        where = {"$and": clauses}
        if self._uses_direct_chroma:
            res = self._safe_direct_call(
                lambda c: c.get(where=where, include=["documents", "metadatas"])
            )
        else:
            res = self._mcp.call_tool("chroma_get_documents", {
                "collection_name": self.collection_name,
                "where": where,
                "include": ["documents", "metadatas"],
            })
        ids = res.get("ids", [])
        docs, metas = res.get("documents", []), res.get("metadatas", [])
        rows = [{"id": i, "text": d, "metadata": m}
                for i, d, m in zip(ids, docs, metas)]
        rows.sort(key=lambda r: r["metadata"].get("start_time", 0))
        return rows

    def distinct_labels(self) -> set[str]:
        """Distinct episode_label values present in this collection — the basis
        for the index build fingerprint (which episodes the index actually
        covers), so drift against the graph can be detected."""
        docs = self._get_all_documents()
        metas = docs.get("metadatas", []) or []
        return {str(m.get("episode_label")) for m in metas
                if m and m.get("episode_label")}

    def count(self) -> int:
        if self._uses_direct_chroma:
            return self._safe_direct_call(lambda c: c.count())
        res = self._mcp.call_tool(
            "chroma_get_collection_count",
            {"collection_name": self.collection_name},
        )
        try:
            return int(res)
        except (TypeError, ValueError):
            return res

    @staticmethod
    def _clean(meta: dict) -> dict:
        """Chroma metadata values must be str/int/float/bool (no None/lists)."""
        out = {}
        for k, v in meta.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                out[k] = v
            else:
                out[k] = str(v)
        return out
