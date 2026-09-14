"""Deterministic ranking of already verified Knowledge claim bundles."""

from __future__ import annotations

from dataclasses import dataclass

from app.context.knowledge_reader import RawKnowledgeBundle
from app.context.retrieval import CandidateReference
from app.retrieval.contracts import CandidateAudit
from app.retrieval.hybrid import (
    CHANNEL_LIMIT,
    RetrievalStrategy,
    bm25_rank,
    reciprocal_rank_fusion,
)
from app.retrieval.ranking import query_terms, rank_scored_candidates
from app.retrieval.ranking import term_coverage as _term_coverage


@dataclass
class RankedKnowledgeBundle:
    bundle: RawKnowledgeBundle
    rank: int
    score: float
    score_breakdown: dict[str, float]
    channels: list[str]
    selected_reason: str


def rank_bundles(
    bundles: list[RawKnowledgeBundle],
    *,
    query: str,
    candidate_references: list[CandidateReference],
    candidate_audits: list[CandidateAudit] | None = None,
    strategy: RetrievalStrategy = "legacy",
) -> list[RankedKnowledgeBundle]:
    """Rank only SQLite-verified bundles; unknown projection IDs have no effect."""

    if strategy != "legacy":
        return _hybrid_bundles(bundles, query, candidate_audits or [], strategy)
    terms = query_terms(query)
    scored: list[tuple[RawKnowledgeBundle, float, dict[str, float], list[str], str]] = []
    for bundle in bundles:
        lexical = _term_coverage(terms, _claim_text(bundle))
        entity_match = _term_coverage(terms, _owner_text(bundle))
        references = candidate_references
        if candidate_audits is not None:
            references = [
                ref for ref, audit in zip(candidate_references, candidate_audits, strict=True)
                if any(match.item_id == bundle.claim["id"] for match in audit.matches)
            ]
        vector_score, graph_score = _projection_scores(bundle, references)
        confidence = float(bundle.claim["confidence"] or 0.5)
        evidence_completeness = min(1.0, len(bundle.evidence) / 2)
        semantic = max(vector_score, graph_score)
        if max(lexical, entity_match, semantic) <= 0.0:
            continue

        breakdown = {
            "lexical_relevance": lexical,
            "semantic_relevance": semantic,
            "task_entity_match": max(entity_match, graph_score),
            "confidence": confidence,
            "evidence_completeness": evidence_completeness,
        }
        score = min(
            1.0,
            0.35 * lexical
            + 0.25 * semantic
            + 0.20 * max(entity_match, graph_score)
            + 0.10 * confidence
            + 0.10 * evidence_completeness,
        )
        channels = ["structured"]
        reasons = ["structured task/project term match"]
        if vector_score:
            channels.append("vector")
            reasons.append("SQLite-revalidated vector chunk candidate")
        if graph_score:
            channels.append("graph")
            reasons.append("SQLite-revalidated graph candidate")
        scored.append((bundle, score, breakdown, channels, "; ".join(reasons)))

    scored = rank_scored_candidates(
        scored,
        score=lambda item: item[1],
        identity=lambda item: (str(item[0].claim["id"]),),
    )
    return [
        RankedKnowledgeBundle(
            bundle=bundle,
            rank=index,
            score=score,
            score_breakdown=breakdown,
            channels=channels,
            selected_reason=reason,
        )
        for index, (bundle, score, breakdown, channels, reason) in enumerate(scored, start=1)
    ]


def _projection_scores(
    bundle: RawKnowledgeBundle, references: list[CandidateReference]
) -> tuple[float, float]:
    chunk_ids = {item.chunk["id"] for item in bundle.evidence}
    entity_ids = set()
    relation_ids = set()
    legacy_entity_ids = set()
    legacy_relation_ids = set()
    if bundle.entity:
        entity_ids.add(bundle.entity["id"])
        if bundle.entity["legacy_id"]:
            legacy_entity_ids.add(bundle.entity["legacy_id"])
    if bundle.relation:
        relation_ids.add(bundle.relation["id"])
        if bundle.relation["legacy_id"]:
            legacy_relation_ids.add(bundle.relation["legacy_id"])

    vector = 0.0
    graph = 0.0
    for reference in references:
        matched = (
            (reference.target_type == "chunk" and reference.target_id in chunk_ids)
            or (reference.target_type == "entity" and reference.target_id in entity_ids)
            or (reference.target_type == "relation" and reference.target_id in relation_ids)
            or (
                reference.target_type == "legacy_entity"
                and reference.target_id in legacy_entity_ids
            )
            or (
                reference.target_type == "legacy_relation"
                and reference.target_id in legacy_relation_ids
            )
        )
        if not matched:
            continue
        score = max(0.0, min(1.0, reference.score))
        if reference.channel == "vector":
            vector = max(vector, score)
        else:
            graph = max(graph, score)
    return vector, graph


def _claim_text(bundle: RawKnowledgeBundle) -> str:
    claim = bundle.claim
    return " ".join(
        str(claim[field]) for field in ("statement", "subject", "predicate", "object_value")
    )


def _hybrid_bundles(bundles, query, audits, strategy):
    if strategy not in {"bm25-v1", "hybrid-v1", "dense-v1"}:
        raise ValueError("Unknown retrieval strategy")
    by_id = {bundle.claim["id"]: bundle for bundle in bundles}
    lexical = bm25_rank(query, {
        key: " ".join([_claim_text(bundle), _owner_text(bundle),
                       *(str(item.chunk["content"]) for item in bundle.evidence)])
        for key, bundle in by_id.items()
    })
    channels = {} if strategy == "dense-v1" else {"bm25": [key for key, _ in lexical]}
    if strategy in {"hybrid-v1", "dense-v1"}:
        for channel in (("vector",) if strategy == "dense-v1" else ("vector", "graph")):
            channels[channel] = list(dict.fromkeys(
                match.item_id for audit in audits
                if audit.channel == channel and audit.status == "verified"
                and (audit.raw_score or 0) > 0
                for match in audit.matches if match.item_id in by_id
            ))[:CHANNEL_LIMIT]
    bm25 = dict(lexical)
    return [RankedKnowledgeBundle(
        bundle=by_id[key], rank=index, score=score,
        score_breakdown={"rrf_score": score, "bm25_score": bm25.get(key, 0),
                         **{f"{name}_rank": rank for name, rank in ranks.items()}},
        channels=list(ranks), selected_reason="SQLite-verified ranked channel fusion",
    ) for index, (key, score, ranks) in enumerate(reciprocal_rank_fusion(channels), start=1)]


def _owner_text(bundle: RawKnowledgeBundle) -> str:
    if bundle.entity:
        return " ".join(
            str(bundle.entity[field])
            for field in ("name", "normalized_name", "entity_type", "domain")
        )
    if bundle.relation:
        return " ".join(
            str(bundle.relation[field])
            for field in ("relation_type", "domain", "source_entity_id", "target_entity_id")
        )
    return ""
