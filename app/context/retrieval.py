"""Optional, non-authoritative candidate retriever contracts."""

from __future__ import annotations

from typing import Protocol

from app.retrieval.contracts import CandidateReference as CandidateReference
from app.retrieval.contracts import CandidateTarget as CandidateTarget


class ContextCandidateRetriever(Protocol):
    """Return IDs only; Context Builder rehydrates every result from SQLite.

    Implementations must bound their own IO. The builder runs synchronously,
    outside database transactions, and retries only transient transport failures.
    """

    def retrieve(
        self,
        query: str,
        *,
        collection_scopes: list[str],
        allowed_document_ids: set[str],
        limit: int,
    ) -> list[CandidateReference]: ...


class QdrantContextCandidateRetriever:
    """Adapt the existing vector projection into untrusted Chunk ID candidates.

    No network call is made until a build request explicitly enables vector
    candidates. The Context Builder will subsequently re-read each ID from
    SQLite and discard stale, unscoped, or ungrounded points.
    """

    def __init__(self, chunk_search=None) -> None:
        self._chunk_search = chunk_search

    def retrieve(
        self,
        query: str,
        *,
        collection_scopes: list[str],
        allowed_document_ids: set[str],
        limit: int,
    ) -> list[CandidateReference]:
        del collection_scopes
        if not allowed_document_ids:
            return []
        if self._chunk_search is None:
            from app.knowledge.query import QdrantKnowledgeSearch

            self._chunk_search = QdrantKnowledgeSearch()
        evidence = self._chunk_search.search(
            query, allowed_paper_ids=allowed_document_ids, top_k=limit
        )
        return [
            CandidateReference(
                channel="vector",
                target_type="chunk",
                target_id=item.chunk_id,
                score=float(item.score),
                source_identity=getattr(item, "source_identity", None),
            )
            for item in evidence
        ]


class Neo4jContextCandidateRetriever:
    """Adapt the existing graph projection into untrusted legacy Core IDs."""

    def __init__(self, graph_search=None) -> None:
        self._graph_search = graph_search

    def retrieve(
        self,
        query: str,
        *,
        collection_scopes: list[str],
        allowed_document_ids: set[str],
        limit: int,
    ) -> list[CandidateReference]:
        del allowed_document_ids
        if not collection_scopes:
            return []
        if self._graph_search is None:
            from app.knowledge.query import Neo4jKnowledgeSearch

            self._graph_search = Neo4jKnowledgeSearch()
        records = self._graph_search.search(query, topic_slugs=collection_scopes, limit=limit)
        references: list[CandidateReference] = []
        for record in records:
            for field, target_type in (
                ("source_id", "legacy_entity"),
                ("target_id", "legacy_entity"),
                ("edge_id", "legacy_relation"),
            ):
                target_id = str(record.get(field) or "")
                if target_id:
                    references.append(
                        CandidateReference(
                            channel="graph", target_type=target_type, target_id=target_id
                        )
                    )
        return references
