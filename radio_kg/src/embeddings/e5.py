"""Local E5 embedding helper for Japanese / multilingual retrieval."""
from __future__ import annotations

import threading
from collections import OrderedDict

from config.settings import settings

# One SentenceTransformer per model name, shared process-wide. The server holds
# 5 VectorStore instances (chunks/summaries/insights/mail/...) — without this
# each one loads its own copy of the same weights (~0.5GB × N RAM, × N startup).
_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = threading.Lock()

# Query-embedding memo: within one QA request the same search_query is encoded
# once for the summary store and again for the chunk store; across requests
# repeated questions hit it too. Keyed (model, text), capped LRU.
_QUERY_VEC_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_QUERY_VEC_CAP = 512
_QUERY_VEC_LOCK = threading.Lock()


class E5Embedder:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.vector_embedding_model

    @property
    def model(self):
        cached = _MODEL_CACHE.get(self.model_name)
        if cached is not None:
            return cached
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(self.model_name)
            if cached is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as e:
                    raise RuntimeError(
                        "sentence-transformers is required for local E5 embeddings. "
                        "Run: cd <Agent workspace root> && uv sync"
                    ) from e
                cached = SentenceTransformer(self.model_name)
                _MODEL_CACHE[self.model_name] = cached
        return cached

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return self._encode([f"passage: {t}" for t in texts])

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float] | None] = []
        misses: list[tuple[int, str]] = []
        with _QUERY_VEC_LOCK:
            for i, t in enumerate(texts):
                vec = _QUERY_VEC_CACHE.get((self.model_name, t))
                if vec is not None:
                    _QUERY_VEC_CACHE.move_to_end((self.model_name, t))
                out.append(vec)
                if vec is None:
                    misses.append((i, t))
        if misses:
            encoded = self._encode([f"query: {t}" for _, t in misses])
            with _QUERY_VEC_LOCK:
                for (i, t), vec in zip(misses, encoded):
                    out[i] = vec
                    _QUERY_VEC_CACHE[(self.model_name, t)] = vec
                    _QUERY_VEC_CACHE.move_to_end((self.model_name, t))
                while len(_QUERY_VEC_CACHE) > _QUERY_VEC_CAP:
                    _QUERY_VEC_CACHE.popitem(last=False)
        return out

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings = self.model.encode(
            texts,
            batch_size=settings.vector_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()
