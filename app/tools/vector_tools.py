from __future__ import annotations

from app.retrieval.rag import build_rag_retriever
from app.schemas.documents import DocumentChunk
from app.schemas.research import ToolResult


def semantic_retrieval(
    query: str,
    chunks: list[dict],
    top_k: int = 5,
    vector_store_provider: str | None = None,
) -> ToolResult:
    """Index provided chunks and retrieve top-k semantically relevant contexts."""

    try:
        parsed_chunks = [DocumentChunk.model_validate(chunk) for chunk in chunks]
        retriever = build_rag_retriever(vector_store_provider=vector_store_provider)
        retriever.index(parsed_chunks)
        hits = retriever.search(query=query, top_k=top_k)

        content = "\n".join(
            f"- {hit.title} | score={hit.score:.3f} | chunk={hit.chunk_id}" for hit in hits
        )
        return ToolResult(
            tool_name="semantic_retrieval",
            status="success",
            content=content,
            metadata={"hits": [hit.model_dump() for hit in hits]},
        )
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return ToolResult(
            tool_name="semantic_retrieval",
            status="error",
            content=str(exc),
            metadata={"hits": []},
        )
