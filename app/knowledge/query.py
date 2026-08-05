from __future__ import annotations

import re
from typing import Any, Protocol

from app.config.settings import Settings, get_settings
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import ReportEvidence


class ChunkSearch(Protocol):
    def search(
        self, query: str, *, allowed_paper_ids: set[str], top_k: int
    ) -> list[ReportEvidence]: ...


class GraphSearch(Protocol):
    def search(self, query: str, *, topic_slugs: list[str], limit: int = 20) -> list[dict]: ...


class QdrantKnowledgeSearch:
    """Retrieve only chunks whose papers exist in SQLite's published knowledge."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def search(
        self, query: str, *, allowed_paper_ids: set[str], top_k: int
    ) -> list[ReportEvidence]:
        if not allowed_paper_ids:
            return []
        from qdrant_client import QdrantClient, models

        from app.retrieval.embeddings import get_embedding_provider

        retrieval_query = (
            _latin_query_terms(query) or query
            if self.settings.embedding_provider == "hash"
            else query
        )
        vector = get_embedding_provider(self.settings).embed_query(retrieval_query)
        response = QdrantClient(url=self.settings.qdrant_url).query_points(
            collection_name=self.settings.knowledge_qdrant_collection,
            query=vector,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="paper_id",
                        match=models.MatchAny(any=sorted(allowed_paper_ids)),
                    )
                ]
            ),
            limit=top_k,
            with_payload=True,
        )
        evidence: list[ReportEvidence] = []
        for point in response.points:
            payload = point.payload or {}
            paper_id = str(payload.get("paper_id", ""))
            if paper_id not in allowed_paper_ids:
                continue
            metadata = payload.get("metadata") or {}
            text = str(payload.get("text", "")).strip()
            if not text:
                continue
            page_start = int(metadata.get("page_start", 1) or 1)
            page_end = int(metadata.get("page_end", page_start) or page_start)
            evidence.append(
                ReportEvidence(
                    id=f"E{len(evidence) + 1}",
                    paper_id=paper_id,
                    chunk_id=str(payload.get("id", point.id)),
                    title=str(payload.get("title", "未命名论文")),
                    text=text,
                    page_start=page_start,
                    page_end=page_end,
                    score=float(point.score),
                )
            )
            if len(evidence) >= top_k:
                break
        return evidence


class Neo4jKnowledgeSearch:
    """Return a bounded approved semantic subgraph for report context."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def search(self, query: str, *, topic_slugs: list[str], limit: int = 20) -> list[dict]:
        from neo4j import GraphDatabase

        terms = _query_terms(query)
        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_username, self.settings.neo4j_password),
        )
        try:
            with driver.session() as session:
                records = session.run(
                    """
                    MATCH (topic:KnowledgeTopicV2)-[:INCLUDES_V2]->(source:KnowledgeEntityV2)
                    WHERE size($topic_slugs) = 0 OR topic.slug IN $topic_slugs
                    OPTIONAL MATCH (source)-[edge:KG_RELATION_V2]-(target:KnowledgeEntityV2)
                    WITH source, edge, target
                    WHERE size($terms) = 0 OR any(term IN $terms WHERE
                        toLower(source.name) CONTAINS term OR
                        toLower(coalesce(source.summary, '')) CONTAINS term OR
                        toLower(coalesce(target.name, '')) CONTAINS term)
                    RETURN source.id AS source_id, source.name AS source_name,
                           edge.id AS edge_id, edge.relation_type AS relation_type,
                           target.id AS target_id, target.name AS target_name
                    LIMIT $limit
                    """,
                    topic_slugs=topic_slugs,
                    terms=terms,
                    limit=limit,
                )
                return [dict(record) for record in records]
        finally:
            driver.close()


class KnowledgeQueryService:
    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        chunk_search: ChunkSearch | None = None,
        graph_search: GraphSearch | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.chunk_search = chunk_search or QdrantKnowledgeSearch(self.settings)
        self.graph_search = graph_search or Neo4jKnowledgeSearch(self.settings)

    def search(
        self, query: str, *, topic_slugs: list[str] | None = None, top_k: int = 8
    ) -> dict[str, Any]:
        selected_topics = list(dict.fromkeys(topic_slugs or []))
        allowed = self.repository.published_paper_ids(selected_topics)
        evidence = self.chunk_search.search(query, allowed_paper_ids=allowed, top_k=top_k)
        graph = self.graph_search.search(
            query, topic_slugs=selected_topics, limit=max(20, top_k * 2)
        )
        return {
            "query": query,
            "topic_slugs": selected_topics,
            "evidence": [item.model_dump() for item in evidence],
            "graph": graph,
        }


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query.casefold())
    return list(dict.fromkeys(terms))[:12]


def _latin_query_terms(query: str) -> str:
    """Keep bilingual technical anchors usable with the local lexical hash embedding."""

    terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", query.casefold())
    return " ".join(dict.fromkeys(terms))
