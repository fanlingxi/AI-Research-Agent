from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import get_settings
from app.graphrag.extractor import GraphExtractor
from app.graphrag.store import InMemoryGraphStore, build_graph_store
from app.retrieval.relevance import relevance_score
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

        if settings.graph_ranking_strategy == "personalized_pagerank":
            graph_paths = self._rerank_with_personalized_pagerank(query, graph_paths)
            graph.metadata["graph_ranking_strategy"] = "personalized_pagerank"

        return KnowledgeBuildOutput(graph=graph, graph_paths=graph_paths)

    def _rerank_with_personalized_pagerank(
        self,
        query: str,
        paths: list[GraphPath],
    ) -> list[GraphPath]:
        """Apply a compact query-personalized PageRank over retrieved path nodes."""

        nodes = {node.id: node for path in paths for node in path.nodes}
        if not nodes:
            return paths
        adjacency: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for path in paths:
            for relation in path.relations:
                if relation.source_id in adjacency and relation.target_id in adjacency:
                    adjacency[relation.source_id].add(relation.target_id)
                    adjacency[relation.target_id].add(relation.source_id)
        seeds = {
            node_id: max(0.01, relevance_score(query, node.name))
            for node_id, node in nodes.items()
        }
        seed_total = sum(seeds.values())
        ranks = {node_id: value / seed_total for node_id, value in seeds.items()}
        for _ in range(12):
            next_ranks = {node_id: 0.15 * seeds[node_id] / seed_total for node_id in nodes}
            for node_id, neighbors in adjacency.items():
                if not neighbors:
                    next_ranks[node_id] += 0.85 * ranks[node_id]
                    continue
                distributed = 0.85 * ranks[node_id] / len(neighbors)
                for neighbor in neighbors:
                    next_ranks[neighbor] += distributed
            ranks = next_ranks
        for path in paths:
            path_rank = sum(ranks[node.id] for node in path.nodes) / len(path.nodes)
            path.score = round(min(1.0, 0.6 * path.score + 2.0 * path_rank), 3)
        return sorted(paths, key=lambda path: path.score, reverse=True)
