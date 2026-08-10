from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.context.models import ContextBuildRequest, runtime_context_token_count
from app.context.repository import SnapshotIntegrityError
from app.context.retrieval import CandidateReference
from app.context.service import (
    ContextBudgetTooSmallError,
    ContextBuilderService,
    ContextConflictError,
)
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan, PublishedEntity, PublishedRelation
from tests.core_fixtures import persist_evidence_chunk


class _VectorCandidates:
    def __init__(self, chunk_id: str) -> None:
        self.chunk_id = chunk_id
        self.calls: list[tuple[str, list[str], set[str]]] = []

    def retrieve(self, query, *, collection_scopes, allowed_document_ids, limit):
        self.calls.append((query, collection_scopes, allowed_document_ids))
        return [
            CandidateReference(
                channel="vector", target_type="chunk", target_id=self.chunk_id, score=0.9
            ),
            CandidateReference(
                channel="vector", target_type="chunk", target_id="stale-qdrant-id", score=1.0
            ),
        ]


def _evidence(document_id: str, suffix: str) -> EvidenceSpan:
    quote = "Context Builder assembles a traceable evidence-grounded package for a task."
    return EvidenceSpan(
        paper_id=document_id,
        chunk_id=f"{document_id}:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote=f"{quote} {suffix}",
    )


def _formal_entity(
    repository: KnowledgeRepository,
    *,
    collection_slug: str,
    entity_id: str,
    title: str,
    suffix: str,
) -> EvidenceSpan:
    ingestion = repository.create_ingestion(
        collection=collection_slug, sources=[f"{entity_id}.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = _evidence(f"paper:{entity_id}", suffix)
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence, title=title)
    entity = PublishedEntity(
            id=entity_id,
            name=title,
            type="Method",
            summary="Context Builder assembles an evidence-grounded Context Package.",
            evidence=[evidence],
            topic_slugs=[collection_slug],
            collection_slugs=[collection_slug],
        )
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO published_entities (
                id, normalized_name, entity_type, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (entity.id, title.casefold(), entity.type, entity.model_dump_json()),
        )
        connection.execute(
            """
            INSERT INTO collection_memberships
                (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
            VALUES ('entity', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (entity.id, ingestion.id, collection_slug),
        )
        repository.core_repository.synchronize_published_entity_tx(connection, entity)
    return evidence


def _formal_relation(
    repository: KnowledgeRepository,
    *,
    collection_slug: str,
    ingestion_id: str,
    relation_id: str,
    source_entity_id: str,
    target_entity_id: str,
    evidence: EvidenceSpan,
) -> PublishedRelation:
    relation = PublishedRelation(
        id=relation_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        type="SUPPORTS",
        summary="Context Builder relation is grounded in the same traceable evidence.",
        confidence=0.9,
        evidence=[evidence],
        topic_slugs=[collection_slug],
        collection_slugs=[collection_slug],
    )
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO published_relations (
                id, source_entity_id, target_entity_id, relation_type,
                payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                relation.id,
                relation.source_entity_id,
                relation.target_entity_id,
                relation.type,
                relation.model_dump_json(),
            ),
        )
        connection.execute(
            """
            INSERT INTO collection_memberships
                (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
            VALUES ('relation', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (relation.id, ingestion_id, collection_slug),
        )
        repository.core_repository.synchronize_published_relation_tx(connection, relation)
    return relation


def _project_with_task(repository: KnowledgeRepository, collection_slug: str):
    memory = repository.memory_repository
    project = memory.create_project(
        name="Context Project",
        goal="Build a traceable Context Builder package",
        domain="engineering",
        metadata={"constraints": ["Use only explicit collection scopes."]},
    )
    scopes = memory.replace_project_knowledge_scopes(
        project.id, [collection_slug], expected_project_revision=project.revision
    )
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Build Context Builder",
        goal="Retrieve evidence-grounded context for the task.",
        priority="high",
        metadata={"expected_output": "A ContextPackage with provenance."},
    )
    memory.create_decision(
        project_id=project.id,
        task_id=task.id,
        summary="Use SQLite as the formal fact authority.",
        rationale="Vector projections may be stale.",
        impact="Context must revalidate every candidate.",
        status="accepted",
        metadata={},
    )
    reference = str(Path(repository.path).with_name("unread-artifact.md"))
    memory.create_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="design",
        reference=reference,
        status="ready",
        metadata={"format": "markdown"},
    )
    return memory.get_project(project.id), task, scopes, reference


def test_context_builder_reads_scoped_formal_knowledge_and_persists_traceable_snapshot(
    tmp_path,
) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    allowed = repository.create_collection("Allowed Context")
    denied = repository.create_collection("Denied Context")
    evidence = _formal_entity(
        repository,
        collection_slug=allowed.slug,
        entity_id="core-allowed-context",
        title="Context Builder",
        suffix="Allowed collection evidence.",
    )
    _formal_entity(
        repository,
        collection_slug=denied.slug,
        entity_id="core-denied-context",
        title="Unrelated Method",
        suffix="Denied collection evidence.",
    )
    project, task, scopes, reference = _project_with_task(repository, allowed.slug)
    vector = _VectorCandidates(evidence.chunk_id)
    builder = ContextBuilderService(repository, vector_retriever=vector)

    package = builder.build(
        ContextBuildRequest(
            task_id=task.id,
            project_id=project.id,
            enable_vector_candidates=True,
            max_tokens=5000,
        )
    )

    assert [item.collection_slug for item in package.project.knowledge_scopes] == [allowed.slug]
    assert package.task.expected_output == "A ContextPackage with provenance."
    assert package.memory.accepted_decisions[0].status == "accepted"
    assert package.artifacts.task_artifacts[0].reference == reference
    assert not Path(reference).exists()
    assert package.diagnostics.knowledge_coverage == "available"
    assert package.diagnostics.vector_candidates == 2
    assert package.token_usage.used <= package.token_usage.budget
    assert len(package.knowledge.claim_bundles) == 1
    bundle = package.knowledge.claim_bundles[0]
    assert bundle.claim.status == "published"
    assert bundle.entity is not None and bundle.entity.legacy_id == "core-allowed-context"
    assert bundle.evidence[0].chunk_id == evidence.chunk_id
    assert bundle.evidence[0].selection.provenance["source_id"] == bundle.sources[0].source_id
    assert "vector" in bundle.selection.retrieval_channels
    assert all(
        bundle.entity is None or bundle.entity.legacy_id != "core-denied-context"
        for bundle in package.knowledge.claim_bundles
    )
    assert vector.calls and vector.calls[0][2] == {evidence.paper_id}

    persisted = builder.snapshot_repository.get(package.snapshot_id)
    items = builder.snapshot_repository.list_items(package.snapshot_id)
    assert persisted.package_sha256 == package.package_sha256
    assert any(
        item.item_type == "claim" and item.item_id == bundle.claim.claim_id for item in items
    )
    assert any(
        item.item_type == "source" and item.provenance["source_id"] == bundle.sources[0].source_id
        for item in items
    )
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM context_snapshot_items").fetchone()[0] >= 8

    constrained = builder.build(
        ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=1000)
    )
    assert constrained.token_usage.used <= constrained.token_usage.budget
    assert constrained.knowledge.claim_bundles == []
    assert constrained.diagnostics.dropped_for_budget == 1


def test_context_builder_fails_closed_without_scope_and_does_not_use_legacy_rows(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Core Scope")
    _formal_entity(
        repository,
        collection_slug=collection.slug,
        entity_id="core-context",
        title="Context Builder",
        suffix="Core evidence.",
    )
    memory = repository.memory_repository
    project = memory.create_project(name="No Scope", goal="Context", domain="test", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Build Context",
        goal="No collection scope is an empty knowledge view.",
        priority="normal",
        metadata={},
    )

    package = ContextBuilderService(repository).build(ContextBuildRequest(task_id=task.id))

    assert package.project.knowledge_scopes == []
    assert package.knowledge.claim_bundles == []
    assert package.diagnostics.knowledge_coverage == "no_scope"
    assert "Project has no explicit Knowledge Collection scope." in package.diagnostics.notices


def test_context_builder_uses_one_connection_for_memory_and_knowledge_reads(
    tmp_path, monkeypatch
) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Consistent Scope")
    _formal_entity(
        repository,
        collection_slug=collection.slug,
        entity_id="core-consistent",
        title="Context Builder",
        suffix="Consistency evidence.",
    )
    project, task, _, _ = _project_with_task(repository, collection.slug)
    builder = ContextBuilderService(repository)
    captured: dict[str, int] = {}
    delayed_ingestion = repository.create_ingestion(
        collection=collection.slug, sources=["late.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.update_ingestion(delayed_ingestion.id, status="needs_review")
    late_evidence = _evidence("paper:late", "Late writer evidence.")
    persist_evidence_chunk(
        repository,
        ingestion_id=delayed_ingestion.id,
        evidence=late_evidence,
        title="Late Context Builder",
    )
    original_memory = repository.memory_repository.snapshot_tx
    original_knowledge = builder.knowledge_reader.read_formal_bundles_tx

    def memory_snapshot(connection, project_id):
        captured["memory"] = id(connection)
        return original_memory(connection, project_id)

    def knowledge_bundles(connection, scopes):
        captured["knowledge"] = id(connection)
        repository.core_repository.synchronize_published_entity(
            PublishedEntity(
                id="core-late-context",
                name="Late Context Builder",
                type="Method",
                summary="Context Builder creates a late but valid Context Package claim.",
                evidence=[late_evidence],
                topic_slugs=[collection.slug],
                collection_slugs=[collection.slug],
            )
        )
        return original_knowledge(connection, scopes)

    monkeypatch.setattr(repository.memory_repository, "snapshot_tx", memory_snapshot)
    monkeypatch.setattr(builder.knowledge_reader, "read_formal_bundles_tx", knowledge_bundles)

    package = builder.build(ContextBuildRequest(task_id=task.id, project_id=project.id))

    assert package.project.project_id == project.id
    assert captured["memory"] == captured["knowledge"]
    assert len(package.knowledge.claim_bundles) == 1
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 2


def test_context_builder_rejects_terminal_task_and_never_creates_partial_snapshot(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Terminal Scope")
    project, task, _, _ = _project_with_task(repository, collection.slug)
    memory = repository.memory_repository
    ready = memory.transition_workspace_task(
        task.id, expected_revision=task.revision, status="ready"
    )
    running = memory.transition_workspace_task(
        ready.id, expected_revision=ready.revision, status="in_progress"
    )
    memory.transition_workspace_task(
        running.id, expected_revision=running.revision, status="completed"
    )

    with pytest.raises(ContextConflictError, match="completed"):
        ContextBuilderService(repository).build(
            ContextBuildRequest(task_id=task.id, project_id=project.id)
        )
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 0


def test_context_scope_follows_authoritative_membership_after_collection_move(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection_a = repository.create_collection("Scope A")
    collection_b = repository.create_collection("Scope B")
    evidence = _formal_entity(
        repository,
        collection_slug=collection_a.slug,
        entity_id="scope-moving-entity",
        title="Context Builder",
        suffix="Initially only in Collection A.",
    )
    _formal_entity(
        repository,
        collection_slug=collection_a.slug,
        entity_id="scope-relation-target",
        title="Context Evidence Target",
        suffix="Relation endpoint remains independently scoped.",
    )
    with repository._connect() as connection:
        ingestion_id = connection.execute(
            """
            SELECT ingestion_id FROM collection_memberships
            WHERE aggregate_type = 'entity' AND aggregate_id = 'scope-moving-entity'
            """
        ).fetchone()[0]
    _formal_relation(
        repository,
        collection_slug=collection_a.slug,
        ingestion_id=ingestion_id,
        relation_id="scope-moving-relation",
        source_entity_id="scope-moving-entity",
        target_entity_id="scope-relation-target",
        evidence=evidence,
    )
    project_a, task_a, _, _ = _project_with_task(repository, collection_a.slug)
    project_b, task_b, _, _ = _project_with_task(repository, collection_b.slug)
    builder = ContextBuilderService(repository)

    before_a = builder.preview_context(
        ContextBuildRequest(task_id=task_a.id, project_id=project_a.id, max_tokens=5000)
    )
    before_b = builder.preview_context(
        ContextBuildRequest(task_id=task_b.id, project_id=project_b.id, max_tokens=5000)
    )
    assert {
        bundle.entity.legacy_id
        for bundle in before_a.knowledge.claim_bundles
        if bundle.entity is not None
    } >= {"scope-moving-entity"}
    assert {
        bundle.relation.legacy_id
        for bundle in before_a.knowledge.claim_bundles
        if bundle.relation is not None
    } == {"scope-moving-relation"}
    moved_claim_ids = {
        bundle.claim.claim_id
        for bundle in before_a.knowledge.claim_bundles
        if (
            bundle.entity is not None and bundle.entity.legacy_id == "scope-moving-entity"
        )
        or (
            bundle.relation is not None and bundle.relation.legacy_id == "scope-moving-relation"
        )
    }
    assert moved_claim_ids
    assert before_b.knowledge.claim_bundles == []

    repository.move_ingestion_collection(str(ingestion_id), collection_b.name)
    with repository._connect() as connection:
        # A move updates the legacy projection, not Core compatibility JSON.
        # The old value must therefore be unable to authorize Context access.
        entity_properties = connection.execute(
            "SELECT properties_json FROM entities WHERE legacy_id = 'scope-moving-entity'"
        ).fetchone()[0]
        relation_properties = connection.execute(
            "SELECT properties_json FROM relations WHERE legacy_id = 'scope-moving-relation'"
        ).fetchone()[0]
    assert collection_a.slug in json.loads(entity_properties)["collection_slugs"]
    assert collection_a.slug in json.loads(relation_properties)["collection_slugs"]

    after_a = builder.preview_context(
        ContextBuildRequest(task_id=task_a.id, project_id=project_a.id, max_tokens=5000)
    )
    after_b = builder.preview_context(
        ContextBuildRequest(task_id=task_b.id, project_id=project_b.id, max_tokens=5000)
    )
    assert all(
        bundle.entity is None or bundle.entity.legacy_id != "scope-moving-entity"
        for bundle in after_a.knowledge.claim_bundles
    )
    assert all(
        bundle.relation is None or bundle.relation.legacy_id != "scope-moving-relation"
        for bundle in after_a.knowledge.claim_bundles
    )
    assert moved_claim_ids.isdisjoint(
        {bundle.claim.claim_id for bundle in after_a.knowledge.claim_bundles}
    )
    moved_entity_bundles = [
        bundle
        for bundle in after_b.knowledge.claim_bundles
        if bundle.entity is not None and bundle.entity.legacy_id == "scope-moving-entity"
    ]
    moved_relation_bundles = [
        bundle
        for bundle in after_b.knowledge.claim_bundles
        if bundle.relation is not None and bundle.relation.legacy_id == "scope-moving-relation"
    ]
    assert moved_entity_bundles and moved_relation_bundles
    assert moved_claim_ids.issubset(
        {bundle.claim.claim_id for bundle in after_b.knowledge.claim_bundles}
    )
    assert all(
        bundle.claim.collection_scopes == [collection_b.slug] for bundle in moved_entity_bundles
    )
    assert all(
        bundle.claim.collection_scopes == [collection_b.slug] for bundle in moved_relation_bundles
    )


def test_context_scope_fails_closed_when_legacy_mapping_or_membership_is_missing(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Fail Closed Scope")
    _formal_entity(
        repository,
        collection_slug=collection.slug,
        entity_id="scope-mapping-required",
        title="Context Builder",
        suffix="Mapping is required for authorization.",
    )
    project, task, _, _ = _project_with_task(repository, collection.slug)
    builder = ContextBuilderService(repository)
    request = ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=5000)
    assert builder.preview_context(request).knowledge.claim_bundles

    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        core_id = connection.execute(
            "SELECT id FROM entities WHERE legacy_id = 'scope-mapping-required'"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM legacy_record_map WHERE legacy_table = 'published_entities' "
            "AND legacy_id = 'scope-mapping-required'"
        )
    assert builder.preview_context(request).knowledge.claim_bundles == []

    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO legacy_record_map
                (legacy_table, legacy_id, core_table, core_id, created_at)
            VALUES
                ('published_entities', 'scope-mapping-required', 'entities', ?, CURRENT_TIMESTAMP)
            """,
            (core_id,),
        )
        connection.execute(
            "DELETE FROM collection_memberships WHERE aggregate_type = 'entity' "
            "AND aggregate_id = 'scope-mapping-required'"
        )
    assert builder.preview_context(request).knowledge.claim_bundles == []


def test_context_budget_uses_one_normalized_runtime_payload_and_is_deterministic(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Budget Scope")
    _formal_entity(
        repository,
        collection_slug=collection.slug,
        entity_id="budget-context",
        title="Context Builder",
        suffix="Budgeted evidence must remain with its Claim.",
    )
    project, task, _, _ = _project_with_task(repository, collection.slug)
    builder = ContextBuilderService(repository)
    request = ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=5000)

    preview = builder.preview_context(request)
    repeat = builder.preview_context(request)
    built = builder.build_context(request)
    assert preview.token_usage.used == runtime_context_token_count(preview)
    assert preview.token_usage.used <= preview.token_usage.budget
    assert preview.token_usage.used == built.token_usage.used == repeat.token_usage.used
    assert [bundle.claim.claim_id for bundle in preview.knowledge.claim_bundles] == [
        bundle.claim.claim_id for bundle in built.knowledge.claim_bundles
    ] == [bundle.claim.claim_id for bundle in repeat.knowledge.claim_bundles]
    assert set(preview.knowledge.model_dump()) == {"claim_bundles"}
    for bundle in preview.knowledge.claim_bundles:
        assert bundle.evidence and bundle.chunks and bundle.documents and bundle.sources
        assert all(evidence.claim_id == bundle.claim.claim_id for evidence in bundle.evidence)
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 1


def test_context_budget_has_a_hard_256_token_limit_and_rejects_oversized_required_payload(
    tmp_path,
) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    project = memory.create_project(name="P", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="T", goal="", priority="normal", metadata={}
    )
    builder = ContextBuilderService(repository)
    compact = builder.preview_context(ContextBuildRequest(task_id=task.id, max_tokens=256))
    assert compact.token_usage.used <= 256
    assert compact.token_usage.used == runtime_context_token_count(compact)

    oversized = memory.create_workspace_task(
        project_id=project.id,
        title="T",
        goal="required-task-context " * 100,
        priority="normal",
        metadata={},
    )
    with pytest.raises(ContextBudgetTooSmallError, match="required Task"):
        builder.preview_context(ContextBuildRequest(task_id=oversized.id, max_tokens=256))


def test_context_snapshot_rejects_tampered_package_json(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Snapshot Integrity")
    project, task, _, _ = _project_with_task(repository, collection.slug)
    builder = ContextBuilderService(repository)
    package = builder.build_context(
        ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=5000)
    )
    tampered = package.model_dump(mode="json")
    tampered["task"]["title"] = "tampered task title"
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE context_snapshots SET package_json = ? WHERE id = ?",
            (json.dumps(tampered, ensure_ascii=False, sort_keys=True), package.snapshot_id),
        )
    with pytest.raises(SnapshotIntegrityError, match="SHA-256"):
        builder.snapshot_repository.get(package.snapshot_id)
