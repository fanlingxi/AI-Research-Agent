from __future__ import annotations

from app.graphrag.reasoner import GraphRAGReasoner
from app.schemas.documents import RetrievalHit
from app.schemas.graph import GraphPath, GraphRAGResult


class GraphReasoningAgent:
    """Synthesize vector hits and graph paths into a GraphRAG answer."""

    def reason(
        self,
        query: str,
        vector_hits: list[RetrievalHit],
        graph_paths: list[GraphPath],
    ) -> GraphRAGResult:
        return GraphRAGReasoner().synthesize(
            query=query,
            vector_hits=vector_hits,
            graph_paths=graph_paths,
        )
