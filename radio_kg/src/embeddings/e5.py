"""Local E5 embedding helper for Japanese / multilingual retrieval."""
from __future__ import annotations

from functools import cached_property

from config.settings import settings


class E5Embedder:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.vector_embedding_model

    @cached_property
    def model(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError(
                "sentence-transformers is required for local E5 embeddings. "
                "Run: cd <Agent workspace root> && uv sync"
            ) from e
        return SentenceTransformer(self.model_name)

    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return self._encode([f"passage: {t}" for t in texts])

    def encode_queries(self, texts: list[str]) -> list[list[float]]:
        return self._encode([f"query: {t}" for t in texts])

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
