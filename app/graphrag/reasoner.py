from __future__ import annotations

from app.schemas.documents import RetrievalHit
from app.schemas.graph import GraphPath, GraphRAGResult


class GraphRAGReasoner:
    """Combine vector evidence and graph paths into a multi-hop answer."""

    def synthesize(
        self,
        query: str,
        vector_hits: list[RetrievalHit],
        graph_paths: list[GraphPath],
    ) -> GraphRAGResult:
        lines = [
            f"GraphRAG reasoning summary for: {query}",
            "",
            "Vector evidence signals:",
        ]

        if vector_hits:
            for index, hit in enumerate(vector_hits[:5], start=1):
                lines.append(f"{index}. {hit.title} (score={hit.score:.3f})")
        else:
            lines.append("No vector evidence was retrieved.")

        lines.extend(["", "Graph multi-hop signals:"])
        if graph_paths:
            for index, path in enumerate(graph_paths[:5], start=1):
                node_names = " -> ".join(node.name for node in path.nodes)
                relation_types = ", ".join(relation.type for relation in path.relations)
                lines.append(
                    f"{index}. {node_names} "
                    f"(relations={relation_types}; score={path.score:.2f})"
                )
        else:
            lines.append("No graph paths were retrieved.")

        lines.extend(
            [
                "",
                "Integrated interpretation:",
                self._interpretation(vector_hits=vector_hits, graph_paths=graph_paths),
            ]
        )

        return GraphRAGResult(
            graph_paths=graph_paths,
            answer="\n".join(lines),
            metadata={
                "vector_hits": len(vector_hits),
                "graph_paths": len(graph_paths),
            },
        )

    def _interpretation(
        self,
        vector_hits: list[RetrievalHit],
        graph_paths: list[GraphPath],
    ) -> str:
        if not vector_hits and not graph_paths:
            return "The system does not yet have enough evidence for GraphRAG reasoning."

        if vector_hits and graph_paths:
            return (
                "The answer is grounded by semantically retrieved chunks and strengthened "
                "by graph paths that connect papers, concepts, methods, datasets, and "
                "evaluation signals. This is the first project point where the system "
                "moves beyond plain RAG into graph-enhanced multi-hop analysis."
            )

        if vector_hits:
            return (
                "The answer is currently supported by vector retrieval only. Graph extraction "
                "should be expanded or Neo4j should be checked if graph paths are expected."
            )

        return (
            "The answer is currently supported by graph structure only. More document chunks "
            "or better embeddings would improve the vector side of the evidence."
        )
