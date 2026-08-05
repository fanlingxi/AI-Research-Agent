import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config.settings import Settings
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import (
    CandidateDecision,
    CandidateEntity,
    CandidateRelation,
    EvidenceSpan,
    MergeSuggestion,
)
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)


def _service(tmp_path, *, projector=None):
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    service = KnowledgeIngestionService(
        repository,
        settings=Settings(
            knowledge_db_path=str(tmp_path / "knowledge.db"),
            knowledge_vault_path=str(tmp_path / "vault"),
        ),
        indexer=NoopChunkIndexer(),
        projector=projector or NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(str(tmp_path / "vault")),
        require_live_llm=False,
    )
    return repository, service


def _evidence() -> EvidenceSpan:
    return EvidenceSpan(
        paper_id="paper:reliable",
        chunk_id="paper:reliable:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Approved evidence remains durable while downstream projections recover.",
    )


def _entity(ingestion, candidate_id: str, name: str, **updates) -> CandidateEntity:
    values = {
        "id": candidate_id,
        "ingestion_id": ingestion.id,
        "topic_slug": ingestion.topic_slug,
        "name": name,
        "type": "Concept",
        "summary": f"{name} 是用于验证可靠审核与正式投影一致性的研究概念。",
        "confidence": 0.9,
        "evidence": _evidence(),
    }
    values.update(updates)
    return CandidateEntity(**values)


def test_schema_migrations_and_sequential_replay_are_idempotent(tmp_path) -> None:
    repository, service = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic="幂等审核", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    repository.add_candidate_entity(_entity(ingestion, "candidate-idempotent", "幂等实体"))

    first = service.decide("candidate-idempotent", CandidateDecision(decision="approve"))
    replay = service.decide("candidate-idempotent", CandidateDecision(decision="approve"))

    assert repository.schema_version() == 6
    assert first.applied and not first.replayed
    assert replay.replayed and not replay.applied
    with pytest.raises(ValueError, match="不同"):
        service.decide("candidate-idempotent", CandidateDecision(decision="reject"))
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM published_entities").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM projection_outbox").fetchone()[0] == 1


def test_concurrent_same_decision_creates_one_fact_event_and_projection(tmp_path) -> None:
    repository, service = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic="并发审核", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    repository.add_candidate_entity(_entity(ingestion, "candidate-concurrent", "并发实体"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: service.decide(
                    "candidate-concurrent", CandidateDecision(decision="approve")
                ),
                range(2),
            )
        )

    assert sum(result.applied for result in results) == 1
    assert sum(result.replayed for result in results) == 1
    with sqlite3.connect(repository.path) as connection:
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("review_events", "published_entities", "projection_outbox")
        ]
    assert counts == [1, 1, 1]


def test_expired_job_lease_is_reclaimed_after_worker_restart(tmp_path) -> None:
    repository, _ = _service(tmp_path)
    repository.create_ingestion(
        topic="租约恢复", sources=["paper.pdf"], pdf_max_pages=3, enqueue=True
    )

    first = repository.claim_job(lease_seconds=-1)
    second = repository.claim_job(lease_seconds=30)

    assert first is not None and second is not None
    assert first.id == second.id
    assert second.attempts == 2


def test_recovery_does_not_steal_an_active_worker_lease(tmp_path) -> None:
    repository, _ = _service(tmp_path)
    repository.create_ingestion(
        topic="活动租约", sources=["paper.pdf"], pdf_max_pages=3, enqueue=True
    )
    active = repository.claim_job(lease_seconds=30)

    repository.recover_running_work()

    assert active is not None
    assert repository.get_job(active.id).status == "running"


class _FailingProjector:
    def upsert_entities(self, entities) -> None:
        raise RuntimeError("neo4j unavailable")

    def upsert_relations(self, relations) -> None:
        raise RuntimeError("neo4j unavailable")


def test_projection_failure_preserves_sqlite_fact_and_can_be_requeued(tmp_path) -> None:
    repository, service = _service(tmp_path, projector=_FailingProjector())
    ingestion = repository.create_ingestion(
        topic="投影恢复", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    repository.add_candidate_entity(_entity(ingestion, "candidate-outbox", "可靠事实"))
    service.decide("candidate-outbox", CandidateDecision(decision="approve"))
    event = repository.claim_projection()

    assert event is not None
    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        service.process_projection(event)
    assert repository.get_published_entity(event.aggregate_id).name == "可靠事实"
    assert repository.get_ingestion(ingestion.id).status == "failed"

    retried = service.retry(ingestion.id)

    assert retried.status == "publishing"
    assert repository.projection_summary()["queued"] == 1


def test_347_candidate_batch_reports_exact_conflicts_and_blocked_relations(tmp_path) -> None:
    repository, service = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic="347 候选验收", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")

    ready_entities = [
        _entity(ingestion, f"candidate-ready-{index}", f"实体 {index}") for index in range(164)
    ]
    for candidate in ready_entities:
        repository.add_candidate_entity(candidate)
    conflicts = []
    for index in range(7):
        candidate = _entity(
            ingestion,
            f"candidate-conflict-{index}",
            f"冲突实体 {index}",
            merge_suggestions=[
                MergeSuggestion(
                    entity_id=f"existing-{index}",
                    name=f"已有实体 {index}",
                    type="Concept",
                    similarity=1.0,
                    match_kind="exact",
                )
            ],
        )
        conflicts.append(candidate)
        repository.add_candidate_entity(candidate)
    for index in range(163):
        repository.add_candidate_relation(
            CandidateRelation(
                id=f"candidate-relation-ready-{index}",
                ingestion_id=ingestion.id,
                topic_slug=ingestion.topic_slug,
                source_candidate_id=ready_entities[index % len(ready_entities)].id,
                target_candidate_id=ready_entities[(index + 1) % len(ready_entities)].id,
                type="SUPPORTS",
                summary="已审核实体之间存在有正式证据支撑的语义关系。",
                confidence=0.88,
                evidence=_evidence(),
            )
        )
    for index in range(13):
        repository.add_candidate_relation(
            CandidateRelation(
                id=f"candidate-relation-blocked-{index}",
                ingestion_id=ingestion.id,
                topic_slug=ingestion.topic_slug,
                source_candidate_id=conflicts[index % len(conflicts)].id,
                target_candidate_id=ready_entities[index].id,
                type="SUPPORTS",
                summary="端点存在实体冲突，因此该关系必须保持阻塞。",
                confidence=0.8,
                evidence=_evidence(),
            )
        )

    result = service.approve_ready(ingestion.id)

    assert result.published_entities == 164
    assert result.published_relations == 0
    assert result.published_entities + result.published_relations == 164
    assert result.skipped_conflicts == 7
    assert result.blocked_relations == 176
    assert result.ingestion.candidate_count == 347
    assert result.ingestion.published_count == 164
    assert result.ingestion.status == "needs_review"
