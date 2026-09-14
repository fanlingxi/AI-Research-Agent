"""Adapt complete SQLite claim bundles to shared candidate bindings and audit."""

from __future__ import annotations

from app.context.knowledge_reader import RawEvidence, RawKnowledgeBundle
from app.context.models import ContextBuildRequest, ContextPackage
from app.context.ranking import RankedKnowledgeBundle
from app.retrieval.contracts import (
    CandidateAudit,
    CandidateMatch,
    RetrievalAudit,
    SelectionAudit,
    SourceIdentity,
)
from app.retrieval.hybrid import PARAMETERS


def evidence_identity(evidence: RawEvidence) -> SourceIdentity:
    return SourceIdentity(
        source_id=evidence.source["id"],
        source_version=evidence.source["version"],
        source_sha256=evidence.source["content_sha256"],
        document_id=evidence.document["id"],
        document_sha256=evidence.document["content_sha256"],
        chunk_id=evidence.chunk["id"],
        chunk_sha256=evidence.chunk["content_sha256"],
    )


def bundle_bindings(
    bundles: list[RawKnowledgeBundle],
) -> dict[tuple[str, str], list[CandidateMatch]]:
    bindings: dict[tuple[str, str], list[CandidateMatch]] = {}
    for bundle in bundles:
        sources = [evidence_identity(item) for item in bundle.evidence]
        targets = {("chunk", source.chunk_id) for source in sources}
        for kind, owner in (("entity", bundle.entity), ("relation", bundle.relation)):
            if owner:
                targets.add((kind, owner["id"]))
                if owner["legacy_id"]:
                    targets.add((f"legacy_{kind}", owner["legacy_id"]))
        for kind, target_id in sorted(targets):
            bindings.setdefault((kind, target_id), []).append(
                CandidateMatch(
                    item_type="claim",
                    item_id=bundle.claim["id"],
                    sources=[s for s in sources if kind != "chunk" or s.chunk_id == target_id],
                )
            )
    return bindings


def context_retrieval_audit(
    request: ContextBuildRequest,
    package: ContextPackage,
    bundles: list[RawKnowledgeBundle],
    ranked: list[RankedKnowledgeBundle],
    candidates: list[CandidateAudit],
) -> RetrievalAudit:
    selected_ids = {item.claim.claim_id for item in package.knowledge.claim_bundles}
    ranked_by_id = {item.bundle.claim["id"]: item for item in ranked}
    selections = []
    structured = []
    for bundle in bundles:
        item_id = bundle.claim["id"]
        item = ranked_by_id.get(item_id)
        selected = item_id in selected_ids
        selections.append(
            SelectionAudit(
                item_type="claim",
                item_id=item_id,
                selected=selected,
                reason=(
                    item.selected_reason
                    if selected
                    else "context_budget"
                    if item
                    else "no_relevance"
                ),
                rank=item.rank if item else None,
                score=item.score if item else None,
                sources=[evidence_identity(e) for e in bundle.evidence],
            )
        )
        structured.append(
            CandidateAudit(
                channel="structured",
                channel_rank=len(structured) + 1,
                target_type="claim",
                target_id=item_id,
                status="verified",
                reason="sqlite_formal_bundle_order",
                matches=[
                    CandidateMatch(
                        item_type="claim", item_id=item_id, sources=selections[-1].sources
                    )
                ],
            )
        )
    audit = RetrievalAudit(
        strategy_id="project-context-legacy-v1",
        parameters={
            "lexical_weight": 0.35,
            "semantic_weight": 0.25,
            "owner_weight": 0.20,
            "confidence_weight": 0.10,
            "completeness_weight": 0.10,
            "term_limit": 12,
            "projection_aggregation": "max_per_channel_clamped_0_1",
            "tie_break": "claim_id",
            "max_context_tokens": request.max_tokens,
            "vector_enabled": request.enable_vector_candidates,
            "graph_enabled": request.enable_graph_candidates,
            "candidate_limit": 40,
            "confidence_fallback": 0.5,
            "evidence_saturation_count": 2,
            "validation_policy": "sqlite-bound-v1",
            "channel_rank_kind": "returned_candidate_position",
        },
        scope={
            "collection_scopes": package.constraints.collection_scopes,
            "project_id": package.project.project_id,
            "task_id": package.task.task_id,
        },
        candidates=structured + candidates,
        selections=selections,
        notices=package.diagnostics.notices,
    )
    if request.retrieval_strategy != "legacy":
        audit.strategy_id = f"project-context-{request.retrieval_strategy}"
        audit.parameters = {
            **PARAMETERS, "max_context_tokens": request.max_tokens,
            "vector_enabled": request.enable_vector_candidates,
            "graph_enabled": request.enable_graph_candidates,
            "validation_policy": "sqlite-bound-v1",
            "rankings": {
                item.bundle.claim["id"]: item.score_breakdown for item in ranked
            },
        }
        lexical_order = sorted(
            (item for item in ranked if "bm25_rank" in item.score_breakdown),
            key=lambda item: item.score_breakdown["bm25_rank"],
        )
        for item in lexical_order:
            audit.candidates.append(CandidateAudit(
                channel="bm25", channel_rank=int(item.score_breakdown["bm25_rank"]),
                target_type="claim", target_id=item.bundle.claim["id"],
                raw_score=item.score_breakdown["bm25_score"], status="verified",
                reason="scoped_sqlite_bm25",
                matches=[CandidateMatch(
                    item_type="claim", item_id=item.bundle.claim["id"],
                    sources=[evidence_identity(e) for e in item.bundle.evidence],
                )],
            ))
    return audit
