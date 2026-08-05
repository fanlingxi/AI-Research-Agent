from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.embeddings import EmbeddingProvider, get_embedding_provider
from app.retrieval.vector_store import VectorStore, build_vector_store
from app.schemas.documents import DocumentChunk, RetrievalHit


@dataclass
class RagRetriever:
    """Indexes chunks and retrieves the most relevant evidence."""

    embedding_provider: EmbeddingProvider
    vector_store: VectorStore

    def index(self, chunks: list[DocumentChunk]) -> None:
        embeddings = self.embedding_provider.embed_documents([chunk.text for chunk in chunks])
        self.vector_store.add_chunks(chunks, embeddings)

    def search(self, query: str, top_k: int = 5) -> list[RetrievalHit]:
        query_embedding = self.embedding_provider.embed_query(query)
        return self.vector_store.search(query_embedding=query_embedding, top_k=top_k)


def build_rag_retriever(vector_store_provider: str | None = None) -> RagRetriever:
    embedding_provider = get_embedding_provider()
    vector_store = build_vector_store(
        provider=vector_store_provider,
    )
    return RagRetriever(embedding_provider=embedding_provider, vector_store=vector_store)
