from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.context.models import (
    ContextBuildRequest,
    ContextPackage,
    canonical_package_sha256,
    runtime_context_payload,
    runtime_context_token_count,
)
from app.context.repository import ContextSnapshotRepository, SnapshotIntegrityError
from app.context.service import ContextBuilderService
from app.knowledge.query import KnowledgeQueryService, _rank_evidence
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.retrieval_audit import rehydrate_graph_candidates
from app.knowledge.schemas import ChunkSearchHit
from app.retrieval.contracts import (
    CandidateMatch,
    CandidateReference,
    SourceIdentity,
    validate_candidates,
)
from tests.test_context_builder import _formal_entity, _formal_relation, _project_with_task
from tests.test_reports import _EvidenceChunkSearch, _repository_with_paper


def _identity():
    return SourceIdentity(
        source_id="source",
        source_version="v1",
        source_sha256="s",
        document_id="doc",
        document_sha256="s",
        chunk_id="chunk",
        chunk_sha256="c",
    )


def test_candidate_binding_keeps_raw_channel_positions_and_rejects_wrong_versions():
    source = _identity()
    bindings = {
        ("chunk", "chunk"): [
            CandidateMatch(
                item_type="claim",
                item_id="claim",
                sources=[source],
            )
        ]
    }
    refs = [
        CandidateReference("vector", "chunk", "missing", 1.0),
        CandidateReference("graph", "chunk", "chunk", 0.4),
        CandidateReference("vector", "chunk", "chunk", 0.7),
        CandidateReference(
            "vector", "chunk", "chunk", 0.9, source.model_copy(update={"source_version": "stale"})
        ),
        CandidateReference("vector", "chunk", "chunk", 0.8, source),
    ]
    audit = validate_candidates(refs, bindings)
    assert [a.channel_rank for a in audit] == [1, 1, 2, 3, 4]
    assert [a.status for a in audit] == ["rejected", "verified", "verified", "rejected", "verified"]
    assert audit[2].reason == "sqlite_bound_provider_version_unknown"
    assert audit[3].reason == "source_version_mismatch"
    assert audit[4].reason == "sqlite_bound_version_matched"
    assert audit[4].matches[0].sources == [source]
    assert validate_candidates(refs, {})[4].matches == []


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_candidate_scores_are_rejected_and_json_safe(score):
    audit = validate_candidates([CandidateReference("vector", "chunk", "chunk", score)], {})[0]
    assert audit.status == "rejected"
    assert audit.reason == "invalid_candidate"
    assert audit.raw_score is None
    json.dumps(audit.model_dump(mode="json"), allow_nan=False)


def test_version_pin_only_matches_its_own_bundle():
    source = _identity()
    other = source.model_copy(update={"source_version": "v2"})
    bindings = {
        ("entity", "entity"): [
            CandidateMatch(item_type="claim", item_id="one", sources=[source]),
            CandidateMatch(item_type="claim", item_id="two", sources=[other]),
        ]
    }
    audit = validate_candidates(
        [CandidateReference("graph", "entity", "entity", 0.5, source)], bindings
    )[0]
    assert [match.item_id for match in audit.matches] == ["one"]


def test_report_selection_audit_distinguishes_noise_diversity_and_count_limits():
    reasons = {}
    _rank_evidence(
        "agent",
        _EvidenceChunkSearch().search("agent", allowed_paper_ids=set(), top_k=1),
        top_k=1,
        selection_reasons=reasons,
    )
    assert reasons["reference"] == "bibliography"
    assert reasons["third"] == "per_source_limit"
    assert reasons["other"] == "top_k_limit"
    assert reasons["main"] == "legacy_score_source_diversity"


def test_report_version_binding_precedes_first_valid_duplicate_selection(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    core = repository.core_repository
    chunk_id = "paper:formal:page:2:chunk:0"
    allowed = repository.published_paper_ids([ingestion.topic_slug])
    _, observations = core.rehydrate_report_candidates(
        [ChunkSearchHit(chunk_id=chunk_id, score=0.5)],
        allowed_paper_ids=allowed,
    )
    identity = observations[0].matches[0].sources[0]
    evidence, audits = core.rehydrate_report_candidates(
        [
            ChunkSearchHit(
                chunk_id=chunk_id,
                score=1.0,
                source_identity=identity.model_copy(update={"source_version": "wrong"}),
            ),
            ChunkSearchHit(chunk_id=chunk_id, score=0.4, source_identity=identity),
            ChunkSearchHit(chunk_id=chunk_id, score=0.9),
            ChunkSearchHit(chunk_id="missing", score=1.0),
        ],
        allowed_paper_ids=allowed,
    )
    assert len(evidence) == 1 and evidence[0].score == 0.4
    assert audits[0].reason == "source_version_mismatch"
    assert audits[2].reason == "duplicate_ignored_first_valid_chunk"
    assert audits[3].status == "rejected"
    assert [a.channel_rank for a in audits] == [1, 2, 3, 4]
    assert (
        core.rehydrate_report_evidence(
            [ChunkSearchHit(chunk_id=chunk_id, score=1.0)], allowed_paper_ids=set()
        )
        == []
    )


class _NoExternalCalls:
    def retrieve(self, *args, **kwargs):
        pytest.fail("External retrieval must not run without verified scope")

    search = retrieve


def test_no_verified_scope_skips_external_services(tmp_path):
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    project = repository.memory_repository.create_project(
        name="Empty", goal="Empty", domain="", metadata={}
    )
    task = repository.memory_repository.create_workspace_task(
        project_id=project.id,
        title="Empty",
        goal="Empty",
        priority="normal",
        metadata={},
    )
    external = _NoExternalCalls()
    package = ContextBuilderService(
        repository,
        vector_retriever=external,
        graph_retriever=external,
    ).build(
        ContextBuildRequest(
            task_id=task.id,
            enable_vector_candidates=True,
            enable_graph_candidates=True,
        )
    )
    assert package.knowledge.claim_bundles == []
    assert package.retrieval_audit.candidates == []
    with pytest.raises(ValueError, match="范围"):
        KnowledgeQueryService(repository, chunk_search=external, graph_search=external).search(
            "query",
            topic_slugs=["missing"],
        )


def _context_setup(tmp_path):
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Audit scope")
    evidence = _formal_entity(
        repository,
        collection_slug=collection.slug,
        entity_id="audit-owner",
        title="Context Builder",
        suffix="Audit evidence.",
    )
    _, task, _, _ = _project_with_task(repository, collection.slug)
    return repository, collection, task, evidence


def test_context_persists_audit_without_changing_runtime_payload_and_checks_tampering(tmp_path):
    repository, _, task, evidence = _context_setup(tmp_path)
    original = ContextBuilderService(repository).preview_context(
        ContextBuildRequest(task_id=task.id)
    )
    source = original.retrieval_audit.selections[0].sources[0]

    class Candidates:
        def retrieve(self, *args, **kwargs):
            return [
                CandidateReference(
                    "vector",
                    "chunk",
                    evidence.chunk_id,
                    1.0,
                    source.model_copy(update={"source_version": "wrong"}),
                ),
                CandidateReference("vector", "chunk", "missing", 1.0),
                CandidateReference("vector", "chunk", evidence.chunk_id, 0.3, source),
            ]

    builder = ContextBuilderService(repository, vector_retriever=Candidates())
    package = builder.build(ContextBuildRequest(task_id=task.id, enable_vector_candidates=True))
    assert package.package_schema_version == "1.1"
    assert package.retrieval_audit.strategy_id == "project-context-legacy-v1"
    projected = [c for c in package.retrieval_audit.candidates if c.channel == "vector"]
    assert [c.status for c in projected] == [
        "rejected",
        "rejected",
        "verified",
    ]
    assert package.knowledge.claim_bundles[0].selection.score_breakdown["semantic_relevance"] == 0.3
    persisted = builder.snapshot_repository.get(package.snapshot_id)
    assert persisted.retrieval_audit == package.retrieval_audit
    legacy_shape = package.model_copy(
        update={"retrieval_audit": None, "package_schema_version": "1.0"}
    )
    assert runtime_context_payload(package) == runtime_context_payload(legacy_shape)
    assert package.token_usage.used == runtime_context_token_count(legacy_shape)
    assert canonical_package_sha256(package) != canonical_package_sha256(legacy_shape)
    tampered = package.model_dump(mode="json")
    tampered["retrieval_audit"]["candidates"][-1]["channel_rank"] = 999
    with repository.database.connect() as connection:
        connection.execute(
            "UPDATE context_snapshots SET package_json=? WHERE id=?",
            (json.dumps(tampered), package.snapshot_id),
        )
    with pytest.raises(SnapshotIntegrityError):
        builder.snapshot_repository.get(package.snapshot_id)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE chunks SET content=content || ' tampered'",
        "UPDATE documents SET content=content || ' tampered'",
        "UPDATE sources SET content_sha256='mismatched-source'",
    ],
)
def test_context_rejects_corrupt_source_binding(tmp_path, mutation):
    repository, _, task, _ = _context_setup(tmp_path)
    with repository.database.connect() as connection:
        connection.execute(mutation)
    package = ContextBuilderService(repository).build(ContextBuildRequest(task_id=task.id))
    assert package.knowledge.claim_bundles == []
    assert package.retrieval_audit.selections == []


def test_budget_dropped_bundles_remain_in_audit(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    package = ContextBuilderService(repository).build(
        ContextBuildRequest(task_id=task.id, max_tokens=1000)
    )
    assert package.knowledge.claim_bundles == []
    assert len(package.retrieval_audit.selections) == 1
    assert package.retrieval_audit.selections[0].reason == "context_budget"
    assert package.retrieval_audit.selections[0].selected is False


def test_actual_pre_a03b_snapshot_blob_preserves_hash_and_reader_compatibility(tmp_path):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/context_snapshot_legacy_v1.json").read_text(
            encoding="utf-8"
        )
    )
    raw = fixture["package"]
    assert "retrieval_audit" not in raw
    package = ContextPackage.model_validate(raw)
    assert canonical_package_sha256(package) == raw["package_sha256"]
    assert runtime_context_payload(package) == fixture["runtime_payload"]
    path = str(tmp_path / "old-snapshot.db")
    # Reproduce the historical blob verbatim; get() only needs these columns.
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE context_snapshots (id TEXT, package_json TEXT, package_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO context_snapshots VALUES (?, ?, ?)",
            (package.snapshot_id, json.dumps(raw), raw["package_sha256"]),
        )
    restored = ContextSnapshotRepository(path).get(package.snapshot_id)
    assert restored.retrieval_audit is None
    assert restored.package_sha256 == raw["package_sha256"]
    with pytest.raises(ValidationError, match="requires retrieval audit"):
        ContextPackage.model_validate({**raw, "package_schema_version": "1.1"})


def test_graph_is_rehydrated_from_scoped_formal_sqlite_and_withdrawal_is_rejected(tmp_path):
    repository, collection, _, evidence = _context_setup(tmp_path)
    target_evidence = _formal_entity(
        repository,
        collection_slug=collection.slug,
        entity_id="audit-target",
        title="Verified Target",
        suffix="Target evidence.",
    )
    with repository.database.connect() as connection:
        ingestion_id = connection.execute(
            "SELECT ingestion_id FROM collection_memberships WHERE aggregate_id='audit-owner'"
        ).fetchone()[0]
    _formal_relation(
        repository,
        collection_slug=collection.slug,
        ingestion_id=ingestion_id,
        relation_id="audit-edge",
        source_entity_id="audit-owner",
        target_entity_id="audit-target",
        evidence=evidence,
    )
    records = [
        {
            "edge_id": "audit-edge",
            "source_id": "spoofed",
            "source_name": "INJECTED",
            "relation_type": "UNTRUSTED",
            "target_name": "FORGED",
        },
        {"edge_id": "missing", "source_name": "OTHER SCOPE"},
        {"edge_id": "audit-edge"},
    ]
    allowed = {evidence.paper_id, target_evidence.paper_id}
    graph, audits, selections = rehydrate_graph_candidates(
        repository,
        records,
        topic_slugs=[collection.slug],
        allowed_paper_ids=allowed,
    )
    assert graph == [
        {
            "edge_id": "audit-edge",
            "source_id": "audit-owner",
            "source_name": "Context Builder",
            "relation_type": "SUPPORTS",
            "target_id": "audit-target",
            "target_name": "Verified Target",
        }
    ]
    assert [a.channel_rank for a in audits] == [1, 2, 3]
    assert audits[1].status == "rejected"
    assert audits[2].reason == "duplicate_ignored_first_valid_relation"
    assert len(selections) == 1
    assert (
        rehydrate_graph_candidates(
            repository, records, topic_slugs=["denied"], allowed_paper_ids=allowed
        )[0]
        == []
    )
    with repository.database.connect() as connection:
        connection.execute("UPDATE relations SET status='retracted' WHERE legacy_id='audit-edge'")
    assert (
        rehydrate_graph_candidates(
            repository, records, topic_slugs=[collection.slug], allowed_paper_ids=allowed
        )[0]
        == []
    )


def test_real_report_query_audit_is_persisted_and_not_sent_to_llm(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)

    class Chunks:
        def search(self, *args, **kwargs):
            return [ChunkSearchHit(chunk_id="paper:formal:page:2:chunk:0", score=0.8)]

    class LLM:
        provider_name = "test"

        def invoke(self, prompt, system_prompt=None):
            assert "quick-report-legacy-v1" not in prompt
            assert "UNTRUSTED_GRAPH" not in prompt
            return (
                "# Report\n\n## Evidence\nEvidence supports synthesis. [E1]\n\n"
                "## Conclusion\nGrounded. [E1]"
            )

    class Graph:
        def search(self, *args, **kwargs):
            return [{"edge_id": "missing", "source_name": "UNTRUSTED_GRAPH"}]

    reports = KnowledgeReportService(
        repository,
        query_service=KnowledgeQueryService(
            repository, chunk_search=Chunks(), graph_search=Graph()
        ),
        llm=LLM(),
        require_live_llm=False,
    )
    report = reports.submit(query="grounded", topic_slugs=[ingestion.topic_slug], top_k=3)
    reports.run(report.id)
    saved = repository.reports.get_report(report.id)
    audit = saved.run_metadata["retrieval_diagnostics"]["retrieval_audit"]
    assert audit["strategy_id"] == "quick-report-legacy-v1"
    assert audit["candidates"][0]["channel_rank"] == 1
    assert audit["selections"][0]["selected"] is True
    assert audit["selections"][0]["sources"][0]["source_version"]
    assert audit["candidates"][1]["channel"] == "graph"
    assert audit["candidates"][1]["status"] == "rejected"
