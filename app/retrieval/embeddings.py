from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

from app.config.settings import Settings, get_settings


class EmbeddingProvider(Protocol):
    """Text embedding interface used by vector stores."""

    dimension: int

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class HashEmbeddingProvider:
    """Deterministic local embedding fallback.

    This is intentionally lightweight. It lets the research pipeline run without
    downloading local models or calling an embedding API. Production runs can
    switch to OpenAI embeddings through configuration.
    """

    dimension: int = 384

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = self._tokens(text)

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector

        return [value / norm for value in vector]

    def _tokens(self, text: str) -> list[str]:
        """Tokenize Latin text and CJK character n-grams without local models."""

        latin_tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        chinese_sequences = re.findall(r"[\u4e00-\u9fff]+", text)
        chinese_tokens = [
            sequence[index : index + width]
            for sequence in chinese_sequences
            for width in (1, 2)
            for index in range(max(0, len(sequence) - width + 1))
        ]
        return latin_tokens + chinese_tokens


@dataclass
class OpenAIEmbeddingProvider:
    """OpenAI embedding adapter used when API credentials are configured."""

    api_key: str
    model: str = "text-embedding-3-small"
    base_url: str = "https://api.openai.com/v1"
    dimension: int = 1536
    request_timeout: float | None = None
    max_retries: int | None = None

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from langchain_openai import OpenAIEmbeddings

        options = {}
        if self.request_timeout is not None:
            options["request_timeout"] = self.request_timeout
        if self.max_retries is not None:
            options["max_retries"] = self.max_retries
        embeddings = OpenAIEmbeddings(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            dimensions=self.dimension,
            **options,
        )
        return embeddings.embed_documents(texts)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    denominator = left_norm * right_norm
    if denominator == 0:
        return 0.0

    return numerator / denominator


def get_embedding_provider(
    settings: Settings | None = None, *,
    request_timeout: float | None = None, max_retries: int | None = None,
) -> EmbeddingProvider:
    settings = settings or get_settings()

    if settings.embedding_provider in {"qwen3-local", "bge-m3-local"}:
        from app.retrieval.neural_embeddings import LocalEmbeddingProvider

        return LocalEmbeddingProvider(
            provider=settings.embedding_provider, dimension=settings.embedding_dimension,
            device=settings.embedding_device, cache_dir=settings.embedding_cache_dir,
            batch_size=settings.embedding_batch_size,
        )

    if settings.embedding_provider == "openai" and settings.openai_api_key:
        return OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            base_url=settings.openai_base_url,
            dimension=settings.embedding_dimension,
            request_timeout=request_timeout,
            max_retries=max_retries,
        )

    return HashEmbeddingProvider(dimension=settings.embedding_dimension)
