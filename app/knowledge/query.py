from __future__ import annotations

import re
from typing import Any, Protocol

from app.config.settings import Settings, get_settings
from app.knowledge.report_inputs import make_read_guard
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.retrieval_audit import rehydrate_graph_candidates_tx
from app.knowledge.schemas import ChunkSearchHit, ReportEvidence
from app.retrieval.contracts import CandidateAudit, CandidateMatch, RetrievalAudit, SelectionAudit
from app.retrieval.hybrid import CHANNEL_LIMIT, PARAMETERS, bm25_rank, reciprocal_rank_fusion
from app.retrieval.neural_embeddings import collection_name
from app.retrieval.policy import (
    IO_TIMEOUT_SECONDS,
    MAX_EXTERNAL_ATTEMPTS,
    MAX_REPORT_SCOPE_ATTEMPTS,
    is_transient_error,
)
from app.retrieval.ranking import query_terms as _query_terms
from app.retrieval.ranking import rank_scored_candidates, term_coverage


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
        vector = get_embedding_provider(
            self.settings, request_timeout=IO_TIMEOUT_SECONDS, max_retries=0,
        ).embed_query(retrieval_query)
        client = QdrantClient(url=self.settings.qdrant_url, timeout=IO_TIMEOUT_SECONDS)
        try:
            response = client.query_points(
                collection_name=collection_name(self.settings),
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
                timeout=IO_TIMEOUT_SECONDS,
            )
        finally:
            client.close()
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
        from neo4j import GraphDatabase, Query

        terms = _query_terms(query)
        driver = GraphDatabase.driver(
            self.settings.neo4j_uri,
            auth=(self.settings.neo4j_username, self.settings.neo4j_password),
            connection_timeout=IO_TIMEOUT_SECONDS,
            connection_acquisition_timeout=IO_TIMEOUT_SECONDS,
            max_transaction_retry_time=0,
        )
        try:
            with driver.session() as session:
                records = session.run(
                    Query("""
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
                    """, timeout=IO_TIMEOUT_SECONDS),
                    topic_slugs=topic_slugs,
                    terms=terms,
                    limit=limit,
                )
                return [dict(record) for record in records]
        finally:
            driver.close()


class KnowledgeRetrievalError(ValueError):
    """A bounded query failed, retaining sanitized attempt accounting."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = list(attempts)


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
        strategy = self.settings.report_retrieval_strategy
        core = self.repository.core_repository
        attempts: list[dict[str, Any]] = []
        for scope_attempt in range(1, MAX_REPORT_SCOPE_ATTEMPTS + 1):
            with self.repository.database.connect() as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                allowed = core.authorized_report_paper_ids_tx(connection, selected_topics)
            if not allowed:
                raise KnowledgeRetrievalError(
                    "所选范围内没有已审核、已发布的论文证据。", attempts,
                )
            warnings: list[str] = []
            hits = []
            if strategy != "bm25-v1":
                try:
                    hits = self._retrieve_projection(
                        "vector", query, selected_topics, allowed,
                        top_k if strategy == "legacy" else CHANNEL_LIMIT, scope_attempt, attempts,
                    )
                except KnowledgeRetrievalError:
                    if strategy in {"legacy", "dense-v1"}:
                        raise
                    warnings.append("向量通道不可用，本次使用SQLite BM25候选。")
            try:
                graph_records = [] if strategy == "dense-v1" else self._retrieve_projection(
                    "graph", query, selected_topics, allowed, top_k, scope_attempt, attempts,
                )
            except KnowledgeRetrievalError:
                graph_records = []
                warnings.append("图关系服务暂时不可用，当前仅展示已定位的原文证据。")

            # Every authoritative value in the response comes from this one
            # snapshot. Projection IO above has no open preparation connection.
            with self.repository.database.connect() as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                current = core.authorized_report_paper_ids_tx(connection, selected_topics)
                if not current:
                    attempts.append({"scope_attempt": scope_attempt, "outcome": "scope_empty"})
                    raise KnowledgeRetrievalError(
                        "所选范围内没有已审核、已发布的论文证据。", attempts,
                    )
                if current != allowed:
                    attempts.append({
                        "scope_attempt": scope_attempt, "outcome": "scope_changed",
                        "before_document_ids": sorted(allowed),
                        "after_document_ids": sorted(current),
                    })
                    continue
                hydrated, candidate_audits = core.rehydrate_report_candidates_tx(
                    connection, hits, allowed_paper_ids=current,
                )
                vector_audits = list(candidate_audits)
                if strategy != "legacy":
                    corpus_ids = core.report_corpus_ids_tx(connection, current)
                    vector_scores = {item.chunk_id: item.score for item in hydrated}
                    hydrated, corpus_audits = core.rehydrate_report_candidates_tx(
                        connection, [ChunkSearchHit(chunk_id=key, score=vector_scores.get(key, 0))
                                     for key in corpus_ids], allowed_paper_ids=current,
                    )
                    candidate_audits.extend(
                        audit.model_copy(update={"channel": "sqlite"}) for audit in corpus_audits
                    )
                try:
                    graph, graph_audits, graph_selections = rehydrate_graph_candidates_tx(
                        connection, graph_records, topic_slugs=selected_topics,
                        allowed_paper_ids=current,
                    )
                except (ValueError, TypeError, KeyError):
                    # Malformed optional graph candidates cannot replace valid
                    # SQLite chunk evidence; database failures still propagate.
                    graph, graph_audits, graph_selections = [], [], []
                    attempts.append({"scope_attempt": scope_attempt, "outcome": "graph_invalid"})
                    warnings.append("图关系候选无法验证，当前仅展示已定位的原文证据。")
                candidate_audits.extend(graph_audits)
                read_guard = make_read_guard(
                    selected_topics, current, hydrated, candidate_audits, graph, graph_selections,
                )
                if strategy != "legacy":
                    read_guard["corpus_chunk_ids"] = corpus_ids
            break
        else:
            raise KnowledgeRetrievalError(
                "检索范围连续两次发生变化，请重新检索。", attempts,
            )
        selection_reasons: dict[str, str] = {}
        hybrid_rankings = {}
        if strategy != "legacy":
            ranked_evidence, hydrated, hybrid_rankings, bm25_audits = _hybrid_evidence(
                query, hydrated, candidate_audits, vector_audits, top_k=top_k,
                selection_reasons=selection_reasons,
                dense_only=strategy == "dense-v1",
            )
            candidate_audits.extend(bm25_audits)
        else:
            ranked_evidence = _rank_evidence(
                query, hydrated, top_k=top_k, selection_reasons=selection_reasons,
            )
        evidence = [
            item.model_copy(update={"id": f"E{index}"})
            for index, item in enumerate(
                ranked_evidence,
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
        identities = {
            match.item_id: match.sources
            for candidate in candidate_audits if candidate.channel in {"vector", "sqlite"}
            for match in candidate.matches
        }
        selected = {item.chunk_id: (rank, item) for rank, item in enumerate(evidence, start=1)}
        selections = []
        for item in hydrated:
            chosen = selected.get(item.chunk_id)
            selections.append(SelectionAudit(
                item_type="chunk", item_id=item.chunk_id, selected=chosen is not None,
                reason=selection_reasons[item.chunk_id],
                rank=chosen[0] if chosen else None,
                score=_evidence_relevance(query, item), sources=identities[item.chunk_id],
            ))
        audit = RetrievalAudit(
            strategy_id="quick-report-legacy-v1",
            parameters={"lexical_weight": 0.25, "term_limit": 12, "score_decimals": 6,
                        "per_paper_limit": 2, "top_k": top_k,
                        "tie_break": ["paper_id", "chunk_id"],
                        "min_relevance": self.settings.report_min_retrieval_relevance,
                        "min_source_diversity": self.settings.report_min_source_diversity,
                        "validation_policy": "sqlite-bound-v1",
                        "channel_rank_kind": "returned_candidate_position",
                        "consistency_policy": "report-read-view-v1",
                        "authorization_policy": "published-core-paper-membership-v1",
                        "scope_attempts": scope_attempt, "attempts": attempts,
                        "max_scope_attempts": MAX_REPORT_SCOPE_ATTEMPTS,
                        "max_external_attempts_per_channel": MAX_EXTERNAL_ATTEMPTS},
            scope={"topic_slugs": selected_topics, "allowed_document_ids": sorted(allowed)},
            candidates=candidate_audits, selections=selections + graph_selections, notices=warnings,
        )
        if strategy != "legacy":
            audit.strategy_id = f"quick-report-{strategy}"
            audit.parameters.update({
                **PARAMETERS, "rankings": hybrid_rankings,
                "gate_score": "max(vector_clamped,bm25/(1+bm25)); legacy lexical coverage",
                "gate_score_calibration": "heuristic_not_semantic_truth",
                "corpus_count": len(corpus_ids),
            })
        return {
            "query": query,
            "topic_slugs": selected_topics,
            "evidence": [item.model_dump() for item in evidence],
            "graph": graph,
            "warnings": warnings,
            "retrieval_diagnostics": {
                "report_input": read_guard,
                "retrieval_audit": audit.model_dump(mode="json"),
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

    def _retrieve_projection(
        self, channel: str, query: str, topics: list[str], allowed: set[str], top_k: int,
        scope_attempt: int, attempts: list[dict[str, Any]],
    ) -> list[Any]:
        for attempt in range(1, MAX_EXTERNAL_ATTEMPTS + 1):
            record = {"scope_attempt": scope_attempt, "channel": channel, "attempt": attempt}
            try:
                if channel == "vector":
                    result = self.chunk_search.search(
                        query, allowed_paper_ids=set(allowed), top_k=top_k,
                    )
                else:
                    result = self.graph_search.search(
                        query, topic_slugs=list(topics), limit=max(20, top_k * 2),
                    )
                result = list(result)
            except Exception as exc:
                transient = is_transient_error(exc)
                attempts.append(record | {
                    "outcome": "transient_failure" if transient else "failure",
                })
                if transient and attempt < MAX_EXTERNAL_ATTEMPTS:
                    continue
                raise KnowledgeRetrievalError(f"{channel}检索服务暂时不可用。", attempts) from None
            attempts.append(record | {"outcome": "returned", "candidate_count": len(result)})
            return result
        raise AssertionError("Retrieval attempt policy must be positive")


def _hybrid_evidence(query, corpus, audits, vector_audits, *, top_k, selection_reasons,
                     dense_only=False):
    by_id = {item.chunk_id: item for item in corpus if not _is_reference_section(item.text)}
    lexical = bm25_rank(query, {key: item.text for key, item in by_id.items()})
    vector = list(dict.fromkeys(
        audit.target_id for audit in vector_audits
        if audit.status == "verified" and (audit.raw_score or 0) > 0
        and audit.target_id in by_id
    ))[:CHANNEL_LIMIT]
    if dense_only:
        lexical = []
    fused = reciprocal_rank_fusion(
        {"vector": vector} if dense_only else {
            "bm25": [key for key, _ in lexical], "vector": vector,
        }
    )
    bm25 = dict(lexical)
    ranks = {key: {"rrf_score": score, "bm25_score": bm25.get(key, 0), **{
        f"{channel}_rank": rank for channel, rank in positions.items()
    }} for key, score, positions in fused}
    relevant = {key: max(min(1.0, max(0.0, item.score)),
                         bm25.get(key, 0) / (1 + bm25.get(key, 0)))
                for key, item in by_id.items()}
    scored = [item.model_copy(update={"score": relevant.get(item.chunk_id, 0)}) for item in corpus]
    by_score = {item.chunk_id: item for item in scored}
    selected = rank_scored_candidates(
        fused, score=lambda item: item[1], identity=lambda item: (item[0],),
        group=lambda item: by_id[item[0]].paper_id, max_per_group=2,
    )[:top_k]
    chosen = {key for key, _, _ in selected}
    for item in corpus:
        selection_reasons[item.chunk_id] = (
            "sqlite_bm25_vector_rrf" if item.chunk_id in chosen else
            "source_or_top_k_limit" if item.chunk_id in ranks else "no_relevance"
        )
    sources = {match.item_id: match.sources for audit in audits for match in audit.matches}
    lexical_audits = [CandidateAudit(
        channel="bm25", channel_rank=index, target_type="chunk", target_id=key,
        raw_score=score, status="verified", reason="scoped_sqlite_bm25",
        matches=[CandidateMatch(item_type="chunk", item_id=key, sources=sources[key])],
    ) for index, (key, score) in enumerate(lexical, start=1)]
    return [by_score[key] for key, _, _ in selected], scored, ranks, lexical_audits


def _latin_query_terms(query: str) -> str:
    """Keep bilingual technical anchors usable with the local lexical hash embedding."""

    terms = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", query.casefold())
    return " ".join(dict.fromkeys(terms))


def _rank_evidence(
    query: str, evidence: list[ReportEvidence], *, top_k: int,
    selection_reasons: dict[str, str] | None = None,
) -> list[ReportEvidence]:
    """Remove bibliography noise and diversify an already scope-filtered result set."""

    terms = _query_terms(query)
    ranked: list[tuple[float, ReportEvidence]] = []
    for item in evidence:
        if _is_reference_section(item.text):
            continue
        score = _evidence_relevance(query, item, terms=terms)
        ranked.append((score, item.model_copy(update={"score": round(score, 6)})))

    ranked = rank_scored_candidates(
        ranked,
        score=lambda item: item[0],
        identity=lambda item: (item[1].paper_id, item[1].chunk_id),
        group=lambda item: item[1].paper_id,
        max_per_group=2,
    )
    selected: list[ReportEvidence] = []
    for _, item in ranked:
        selected.append(item)
        if len(selected) >= top_k:
            break
    if selection_reasons is not None:
        eligible = {item.chunk_id for _, item in ranked}
        selected_ids = {item.chunk_id for item in selected}
        for item in evidence:
            selection_reasons[item.chunk_id] = (
                "legacy_score_source_diversity" if item.chunk_id in selected_ids else
                "bibliography" if _is_reference_section(item.text) else
                "per_source_limit" if item.chunk_id not in eligible else "top_k_limit"
            )
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
    coverage = term_coverage(terms, item.text)
    return round(min(1.0, max(0.0, float(item.score)) + 0.25 * coverage), 6)
