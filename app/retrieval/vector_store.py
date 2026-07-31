from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.config.settings import Settings, get_settings
from app.retrieval.embeddings import cosine_similarity
from app.schemas.documents import DocumentChunk, RetrievalHit


class VectorStore(Protocol):
    """Minimal vector store interface for RAG retrieval."""

    provider_name: str

    def add_chunks(self, chunks: list[DocumentChunk], embeddings: list[list[float]]) -> None:
        ...

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[RetrievalHit]:
        ...


@dataclass
class InMemoryVectorStore:
    """Local vector store for fast demos and tests."""

    provider_name: str = "memory"
    _items: list[tuple[DocumentChunk, list[float]]] = field(default_factory=list)

    def add_chunks(self, chunks: list[DocumentChunk], embeddings: list[list[float]]) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length.")

        self._items.extend(zip(chunks, embeddings, strict=True))

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[RetrievalHit]:
        scored: list[RetrievalHit] = []
        for chunk, embedding in self._items:
            scored.append(
                RetrievalHit(
                    chunk_id=chunk.id,
                    paper_id=chunk.paper_id,
                    title=chunk.title,
                    text=chunk.text,
                    score=cosine_similarity(query_embedding, embedding),
                    metadata=chunk.metadata,
                )
            )

        return sorted(scored, key=lambda hit: hit.score, reverse=True)[:top_k]


class QdrantVectorStore:
    """Qdrant-backed vector store for local Docker or remote Qdrant."""

    provider_name = "qdrant"

    def __init__(self, url: str, collection_name: str, vector_size: int) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models

        self.client = QdrantClient(url=url)
        self.collection_name = collection_name
        self.vector_size = vector_size
        self.models = models
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        collections = self.client.get_collections().collections
        exists = any(collection.name == self.collection_name for collection in collections)
        if exists:
            return

        self.client.create_collection(
            collection_name=self.collection_name,
            vectors_config=self.models.VectorParams(
                size=self.vector_size,
                distance=self.models.Distance.COSINE,
            ),
        )

    def add_chunks(self, chunks: list[DocumentChunk], embeddings: list[list[float]]) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must have the same length.")

        points = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, chunk.id))
            payload = chunk.model_dump()
            points.append(
                self.models.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload,
                )
            )

        self.client.upsert(collection_name=self.collection_name, points=points)

    def search(self, query_embedding: list[float], top_k: int = 5) -> list[RetrievalHit]:
        results = self.client.search(
            collection_name=self.collection_name,
            query_vector=query_embedding,
            limit=top_k,
        )

        hits: list[RetrievalHit] = []
        for item in results:
            payload = item.payload or {}
            metadata = payload.get("metadata") or {}
            hits.append(
                RetrievalHit(
                    chunk_id=payload.get("id", str(item.id)),
                    paper_id=payload.get("paper_id", "unknown"),
                    title=payload.get("title", "Untitled"),
                    text=payload.get("text", ""),
                    score=float(item.score),
                    metadata=metadata,
                )
            )
        return hits


def build_vector_store(
    provider: str | None = None,
    settings: Settings | None = None,
) -> VectorStore:
    settings = settings or get_settings()
    selected_provider = provider or settings.vector_store_provider

    if selected_provider == "qdrant":
        return QdrantVectorStore(
            url=settings.qdrant_url,
            collection_name=settings.qdrant_collection,
            vector_size=settings.embedding_dimension,
        )

    return InMemoryVectorStore()
