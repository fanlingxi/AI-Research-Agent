from __future__ import annotations

from app.agents.graph_reasoning_agent import GraphReasoningAgent
from app.agents.knowledge_agent import KnowledgeAgent
from app.schemas.documents import DocumentChunk, RetrievalHit
from app.schemas.graph import GraphPath
from app.schemas.research import ToolResult


def build_knowledge_graph(
    query: str,
    chunks: list[dict],
    graph_store_provider: str | None = None,
) -> ToolResult:
    """Extract entities and relations, then retrieve graph context."""

    try:
        parsed_chunks = [DocumentChunk.model_validate(chunk) for chunk in chunks]
        result = KnowledgeAgent().build_and_retrieve(
            query=query,
            chunks=parsed_chunks,
            graph_store_provider=graph_store_provider,
        )
        return ToolResult(
            tool_name="build_knowledge_graph",
            status="success",
            content=(
                f"Built graph with {len(result.graph.entities)} entities, "
                f"{len(result.graph.relations)} relations, and "
                f"{len(result.graph_paths)} retrieved paths."
            ),
            metadata={
                "graph": result.graph.model_dump(),
                "graph_paths": [path.model_dump() for path in result.graph_paths],
            },
        )
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return ToolResult(
            tool_name="build_knowledge_graph",
            status="error",
            content=str(exc),
            metadata={"graph_paths": []},
        )


def graph_rag_reasoning(
    query: str,
    vector_hits: list[dict],
    graph_paths: list[dict],
) -> ToolResult:
    """Combine vector hits and graph paths into a GraphRAG answer."""

    hits = [RetrievalHit.model_validate(hit) for hit in vector_hits]
    paths = [GraphPath.model_validate(path) for path in graph_paths]
    result = GraphReasoningAgent().reason(query=query, vector_hits=hits, graph_paths=paths)
    return ToolResult(
        tool_name="graph_rag_reasoning",
        status="success",
        content=result.answer,
        metadata=result.model_dump(),
    )
