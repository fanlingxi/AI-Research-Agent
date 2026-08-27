from __future__ import annotations

import re
from typing import Any, Protocol

from app.config.settings import Settings, get_settings
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import ChunkSearchHit, ReportEvidence


class ChunkSearch(Protocol):
    def search(
        self, query: str, *, allowed_paper_ids: set[str], top_k: int
    ) -> list[ChunkSearchHit]: ...


class GraphSearch(Protocol):
    def search(self, query: str, *, topic_slugs: list[str], limit: int = 20) -> list[dict]: ...


class QdrantKnowledgeSearch:
    """Retrieve only chunks whose papers exist in SQLite's published knowledge."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def search(
        self, query: str, *, allowed_paper_ids: set[str], top_k: int
    ) -> list[ChunkSearchHit]:
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
            # Retrieve a modest candidate pool first. The final evidence list
            # is filtered and diversified below, so a reference section or a
            # single long survey cannot consume the entire user-visible page.
            limit=min(max(top_k * 6, top_k), 100),
            # The filter and returned ID are candidate-generation hints only.
            # Text, title, paper identity, pages, and authorization are re-read
            # from SQLite below; vector payload content never becomes evidence.
            with_payload=["id"],
        )
        hits: list[ChunkSearchHit] = []
        for point in response.points:
            payload = point.payload or {}
            chunk_id = str(payload.get("id") or "").strip()
            if not chunk_id:
                continue
            hits.append(ChunkSearchHit(chunk_id=chunk_id, score=float(point.score)))
        return hits


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
                    MATCH (source)-[edge:KG_RELATION_V2]-(target:KnowledgeEntityV2)
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
        hits = self.chunk_search.search(query, allowed_paper_ids=allowed, top_k=top_k)
        hydrated = self.repository.core_repository.rehydrate_report_evidence(
            hits,
            allowed_paper_ids=allowed,
        )
        evidence = [
            item.model_copy(update={"id": f"E{index}"})
            for index, item in enumerate(
                _rank_evidence(query, hydrated, top_k=top_k),
                start=1,
            )
        ]
        relevant_papers = {
            item.paper_id
            for item in hydrated
            if not _is_reference_section(item.text)
            and _evidence_relevance(query, item) >= self.settings.report_min_retrieval_relevance
        }
        selected_relevant = [
            item
            for item in evidence
            if _evidence_relevance(query, item) >= self.settings.report_min_retrieval_relevance
        ]
        required_sources = (
            self.settings.report_min_source_diversity
            if len(relevant_papers) >= self.settings.report_min_source_diversity
            else 0
        )
        graph: list[dict] = []
        warnings: list[str] = []
        try:
            graph = self.graph_search.search(
                query, topic_slugs=selected_topics, limit=max(20, top_k * 2)
            )
        except Exception:
            # The graph is a supporting projection. SQLite/Qdrant evidence is
            # still valid and useful when Neo4j is unavailable.
            warnings.append("图关系服务暂时不可用，当前仅展示已定位的原文证据。")
        return {
            "query": query,
            "topic_slugs": selected_topics,
            "evidence": [item.model_dump() for item in evidence],
            "graph": graph,
            "warnings": warnings,
            "retrieval_diagnostics": {
                "candidate_count": len(hits),
                "rehydrated_count": len(hydrated),
                "selected_count": len(evidence),
                "relevant_paper_count": len(relevant_papers),
                "selected_paper_count": len({item.paper_id for item in evidence}),
                "required_source_count": required_sources,
                "retrieval_relevance": (
                    len(selected_relevant) / len(evidence) if evidence else 0.0
                ),
            },
        }


def _query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query.casefold())
    return list(dict.fromkeys(terms))[:12]


def _latin_query_terms(query: str) -> str:
    """Keep bilingual technical anchors usable with the local lexical hash embedding."""

    terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", query.casefold())
    return " ".join(dict.fromkeys(terms))


def _rank_evidence(
    query: str, evidence: list[ReportEvidence], *, top_k: int
) -> list[ReportEvidence]:
    """Remove bibliography noise and diversify an already scope-filtered result set."""

    terms = _query_terms(query)
    ranked: list[tuple[float, ReportEvidence]] = []
    for item in evidence:
        if _is_reference_section(item.text):
            continue
        score = _evidence_relevance(query, item, terms=terms)
        ranked.append((score, item.model_copy(update={"score": round(score, 6)})))

    ranked.sort(key=lambda item: (-item[0], item[1].paper_id, item[1].chunk_id))
    selected: list[ReportEvidence] = []
    per_paper: dict[str, int] = {}
    for _, item in ranked:
        if per_paper.get(item.paper_id, 0) >= 2:
            continue
        selected.append(item)
        per_paper[item.paper_id] = per_paper.get(item.paper_id, 0) + 1
        if len(selected) >= top_k:
            break
    return selected


def _is_reference_section(text: str) -> bool:
    return bool(re.match(r"^\s*(?:references|bibliography|参考文献)\b", text, flags=re.IGNORECASE))


def _evidence_relevance(
    query: str,
    item: ReportEvidence,
    *,
    terms: list[str] | None = None,
) -> float:
    terms = _query_terms(query) if terms is None else terms
    normalized = item.text.casefold()
    coverage = sum(term in normalized for term in terms) / len(terms) if terms else 0.0
    return round(min(1.0, max(0.0, float(item.score)) + 0.25 * coverage), 6)
