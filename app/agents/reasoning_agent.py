from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import get_settings
from app.retrieval.rag import build_rag_retriever
from app.retrieval.vector_store import InMemoryVectorStore
from app.schemas.documents import DocumentChunk, RetrievalHit


@dataclass
class ReasoningResult:
    hits: list[RetrievalHit]
    answer: str
    vector_store_provider: str


class ReasoningAgent:
    """Run Phase 2 RAG retrieval and generate an evidence summary."""

    def retrieve_and_answer(
        self,
        query: str,
        chunks: list[DocumentChunk],
        top_k: int | None = None,
        vector_store_provider: str | None = None,
    ) -> ReasoningResult:
        settings = get_settings()
        selected_top_k = top_k or settings.retrieval_top_k
        selected_provider = vector_store_provider or settings.vector_store_provider

        try:
            retriever = build_rag_retriever(vector_store_provider=selected_provider)
            provider_name = retriever.vector_store.provider_name
        except Exception:
            retriever = build_rag_retriever(vector_store_provider="memory")
            retriever.vector_store = InMemoryVectorStore()
            provider_name = "memory"

        retriever.index(chunks)
        hits = retriever.search(query=query, top_k=selected_top_k)
        return ReasoningResult(
            hits=hits,
            answer=self._build_answer(query=query, hits=hits),
            vector_store_provider=provider_name,
        )

    def _build_answer(self, query: str, hits: list[RetrievalHit]) -> str:
        if not hits:
            return "No relevant chunks were retrieved yet."

        lines = [
            f"RAG evidence summary for: {query}",
            "",
            "Most relevant evidence:",
        ]
        for index, hit in enumerate(hits, start=1):
            snippet = hit.text.replace("\n", " ")[:420]
            lines.append(f"{index}. {hit.title} (score={hit.score:.3f}) — {snippet}")

        lines.extend(
            [
                "",
                "Phase 2 interpretation:",
                "The system can now collect candidate papers, convert them into chunks, "
                "index them in a vector store, and retrieve evidence for downstream "
                "report writing. "
                "Phase 3 will replace this evidence-only reasoning with GraphRAG "
                "traversal over Neo4j.",
            ]
        )
        return "\n".join(lines)
