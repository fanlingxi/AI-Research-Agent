from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import get_settings
from app.graphrag.extractor import GraphExtractor
from app.graphrag.store import InMemoryGraphStore, build_graph_store
from app.schemas.documents import DocumentChunk
from app.schemas.graph import GraphBuildResult, GraphPath


@dataclass
class KnowledgeBuildOutput:
    graph: GraphBuildResult
    graph_paths: list[GraphPath]


class KnowledgeAgent:
    """Extract entities and relations, persist them, and retrieve graph context."""

    def build_and_retrieve(
        self,
        query: str,
        chunks: list[DocumentChunk],
        graph_store_provider: str | None = None,
    ) -> KnowledgeBuildOutput:
        settings = get_settings()
        extractor = GraphExtractor(max_entities_per_chunk=settings.graph_entities_per_chunk)
        graph = extractor.extract(chunks)

        selected_provider = graph_store_provider or settings.graph_store_provider
        try:
            if selected_provider == "memory":
                store = InMemoryGraphStore()
            else:
                store = build_graph_store(provider=selected_provider, settings=settings)
            store.upsert_graph(entities=graph.entities, relations=graph.relations)
            graph_paths = store.retrieve_paths(
                query=query,
                max_hops=settings.graph_max_hops,
                limit=settings.graph_top_k,
            )
            graph.graph_store_provider = store.provider_name
        except Exception as exc:
            store = InMemoryGraphStore()
            store.upsert_graph(entities=graph.entities, relations=graph.relations)
            graph_paths = store.retrieve_paths(
                query=query,
                max_hops=settings.graph_max_hops,
                limit=settings.graph_top_k,
            )
            graph.graph_store_provider = "memory"
            graph.metadata["graph_store_error"] = str(exc)

        return KnowledgeBuildOutput(graph=graph, graph_paths=graph_paths)
