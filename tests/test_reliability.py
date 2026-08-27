import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository, StaleIngestionExecution
from app.knowledge.schemas import (
    CandidateDecision,
    CandidateEntity,
    CandidateRelation,
    EvidenceSpan,
    KnowledgeExtraction,
    MergeSuggestion,
    PaperReading,
)
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.llms.provider import MockLLMClient
from app.schemas.documents import ParsedDocument
from app.worker import KnowledgeWorker
from tests.core_fixtures import persist_evidence_chunk


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


class _SlowExtractor:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    def extract(self, paper, chunks):
        self.calls += 1
        time.sleep(self.delay)
        evidence = EvidenceSpan(
            paper_id=paper.id,
            chunk_id=chunks[0].id,
            page_start=1,
            page_end=1,
            quote="A durable ingestion lease prevents duplicate extraction work.",
        )
        return ExtractionResult(
            KnowledgeExtraction(
                reading=PaperReading(
                    research_problem="验证长时间知识入库任务不会被其他执行器重复领取。",
                    core_contributions=["为入库执行增加租约续期与尝试代隔离。"],
                    method_summary="使用短租约和慢抽取器验证同一任务只有一个执行代。",
                    evidence=evidence,
                )
            )
        )


def _slow_parser(*, source: str, max_pages: int) -> ParsedDocument:
    text = "A durable ingestion lease prevents duplicate extraction work."
    return ParsedDocument(
        source=source,
        title="Durable Ingestion Paper",
        text=text,
        pages=1,
        page_texts=[text],
        page_numbers=[1],
    )


def _claimed_ingestion_service(tmp_path, extractor):
    repository = KnowledgeRepository(str(tmp_path / "claimed.db"))
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "claimed-vault"),
        chunk_size=40,
        chunk_overlap=10,
    )
    service = KnowledgeIngestionService(
        repository,
        settings=settings,
        extractor=extractor,
        parser=_slow_parser,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(settings.knowledge_vault_path),
        require_live_llm=False,
    )
    ingestion = repository.create_ingestion(
        topic="入库租约测试",
        sources=["slow.pdf"],
        pdf_max_pages=150,
    )
    return repository, service, ingestion


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
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())
    repository.add_candidate_entity(_entity(ingestion, "candidate-idempotent", "幂等实体"))

    first = service.decide("candidate-idempotent", CandidateDecision(decision="approve"))
    replay = service.decide("candidate-idempotent", CandidateDecision(decision="approve"))

    assert repository.schema_version() == 18
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
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())
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


def test_unified_worker_claims_interactive_priority_before_older_batch_work(tmp_path) -> None:
    repository, _ = _service(tmp_path)
    background = repository.create_ingestion(
        topic="后台批处理", sources=["older.pdf"], pdf_max_pages=3, enqueue=True
    )
    interactive = repository.create_ingestion(
        topic="交互式任务",
        sources=["newer.pdf"],
        pdf_max_pages=3,
        auto_execute=True,
    )

    claimed = repository.claim_job(lease_seconds=30, owner_id="worker-priority")

    assert claimed is not None
    assert claimed.resource_id == interactive.id
    assert claimed.priority > repository.get_resource_job("ingestion", background.id).priority
    assert repository.get_resource_job("ingestion", background.id).status == "queued"


def test_job_fencing_requires_attempt_and_lease_owner(tmp_path) -> None:
    repository, _ = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic="所有者隔离", sources=["paper.pdf"], pdf_max_pages=3, enqueue=True
    )
    claimed = repository.claim_resource_job(
        "ingestion", ingestion.id, lease_seconds=30, owner_id="worker-alpha"
    )
    assert claimed is not None

    assert not repository.renew_job_lease(
        claimed.id,
        expected_attempt=claimed.attempts,
        expected_owner="worker-beta",
        lease_seconds=30,
    )
    assert not repository.complete_job(
        claimed.id,
        expected_attempt=claimed.attempts,
        expected_owner="worker-beta",
    )
    assert not repository.fail_job(
        claimed.id,
        "stale executor",
        expected_attempt=claimed.attempts,
        expected_owner="worker-beta",
    )
    assert repository.get_job(claimed.id).status == "running"
    assert repository.complete_job(
        claimed.id,
        expected_attempt=claimed.attempts,
        expected_owner="worker-alpha",
    )


def test_ingestion_heartbeat_prevents_duplicate_long_execution(tmp_path) -> None:
    extractor = _SlowExtractor(delay=1.4)
    repository, service, ingestion = _claimed_ingestion_service(tmp_path, extractor)
    claimed = repository.claim_resource_job("ingestion", ingestion.id, lease_seconds=1)
    assert claimed is not None

    thread = threading.Thread(
        target=service.execute_claimed_with_heartbeat,
        args=(claimed,),
        kwargs={"lease_seconds": 1},
    )
    thread.start()
    time.sleep(1.1)
    duplicate = repository.claim_resource_job("ingestion", ingestion.id, lease_seconds=1)
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert duplicate is None
    assert extractor.calls == 1
    assert repository.get_resource_job("ingestion", ingestion.id).attempts == 1
    assert repository.get_ingestion(ingestion.id).status == "needs_review"
    assert len(repository.list_candidates(ingestion.id)) == 1


def test_recovered_ingestion_replaces_partial_draft_candidates(tmp_path) -> None:
    extractor = _SlowExtractor()
    repository, service, ingestion = _claimed_ingestion_service(tmp_path, extractor)
    first = repository.claim_resource_job("ingestion", ingestion.id, lease_seconds=-1)
    assert first is not None
    parsed = _slow_parser(source="slow.pdf", max_pages=150)
    paper = service._paper_from_document(parsed, "slow.pdf")
    partial = _entity(
        ingestion,
        "candidate-partial",
        "崩溃前半成品",
        evidence=EvidenceSpan(
            paper_id=paper.id,
            chunk_id=f"{paper.id}:page:1:chunk:0",
            page_start=1,
            page_end=1,
            quote="A durable ingestion lease prevents duplicate extraction work.",
        ),
    )
    repository.add_candidate_entity(partial)
    recovered = repository.claim_resource_job("ingestion", ingestion.id, lease_seconds=30)
    assert recovered is not None and recovered.attempts == 2

    result = service.execute_claimed(recovered)

    assert result.status == "needs_review"
    candidates = repository.list_candidates(ingestion.id)
    assert len(candidates) == 1
    assert candidates[0]["candidate"]["name"] == "Durable Ingestion Paper"


def test_stale_ingestion_attempt_cannot_write_or_finalize(tmp_path) -> None:
    repository, service, ingestion = _claimed_ingestion_service(tmp_path, _SlowExtractor())
    stale = repository.claim_resource_job("ingestion", ingestion.id, lease_seconds=-1)
    current = repository.claim_resource_job("ingestion", ingestion.id, lease_seconds=30)
    assert stale is not None and current is not None

    with pytest.raises(StaleIngestionExecution):
        service.run(
            ingestion.id,
            expected_job_id=stale.id,
            expected_job_attempt=stale.attempts,
        )

    assert repository.list_candidates(ingestion.id) == []
    assert repository.get_resource_job("ingestion", ingestion.id).attempts == 2
    assert service.execute_claimed(current).status == "needs_review"


def test_claimed_ingestion_with_mock_llm_fails_before_any_extraction_side_effect(
    tmp_path,
) -> None:
    repository = KnowledgeRepository(str(tmp_path / "mock-claimed.db"))
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "mock-claimed-vault"),
        chunk_size=40,
        chunk_overlap=10,
        llm_provider="mock",
    )
    extractor = _SlowExtractor()
    parser_calls: list[str] = []
    indexed_batches: list[list] = []

    def parser(*, source: str, max_pages: int) -> ParsedDocument:
        parser_calls.append(source)
        return _slow_parser(source=source, max_pages=max_pages)

    class RecordingIndexer:
        def index(self, chunks) -> None:
            indexed_batches.append(chunks)

    service = KnowledgeIngestionService(
        repository,
        settings=settings,
        llm=MockLLMClient(),
        extractor=extractor,
        parser=parser,
        indexer=RecordingIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(settings.knowledge_vault_path),
        require_live_llm=True,
    )
    ingestion = repository.create_ingestion(
        topic="恢复任务 live LLM 门禁",
        sources=["must-not-parse.pdf"],
        pdf_max_pages=150,
        auto_execute=True,
    )
    claimed = repository.claim_dispatched_ingestion_job(lease_seconds=30)
    assert claimed is not None

    result = service.execute_claimed(claimed)

    assert result.status == "failed"
    assert "真实 API Key" in (result.error or "")
    assert result.document_count == 0
    assert result.candidate_count == 0
    assert parser_calls == []
    assert indexed_batches == []
    assert extractor.calls == 0
    assert repository.list_candidates(ingestion.id) == []
    job = repository.get_resource_job("ingestion", ingestion.id)
    assert job.status == "failed"
    assert job.attempts == 1
    assert "真实 API Key" in (job.last_error or "")


class _FailingProjector:
    def upsert_entities(self, entities) -> None:
        raise RuntimeError("neo4j unavailable")

    def upsert_relations(self, relations) -> None:
        raise RuntimeError("neo4j unavailable")


class _SlowProjector:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    def upsert_entities(self, entities) -> None:
        self.calls += 1
        time.sleep(self.delay)

    def upsert_relations(self, relations) -> None:
        self.calls += 1
        time.sleep(self.delay)


def test_projection_failure_preserves_sqlite_fact_and_can_be_requeued(tmp_path) -> None:
    repository, service = _service(tmp_path, projector=_FailingProjector())
    ingestion = repository.create_ingestion(
        topic="投影恢复", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())
    repository.add_candidate_entity(_entity(ingestion, "candidate-outbox", "可靠事实"))
    service.decide("candidate-outbox", CandidateDecision(decision="approve"))
    event = repository.claim_projection()

    assert event is not None
    with pytest.raises(RuntimeError, match="neo4j unavailable"):
        service.process_projection(event)
    assert repository.get_published_entity(event.aggregate_id).name == "可靠事实"
    assert repository.get_ingestion(ingestion.id).status == "failed"
    with sqlite3.connect(repository.path) as connection:
        outbox_status = connection.execute(
            "SELECT status, last_error FROM projection_outbox WHERE id = ?", (event.id,)
        ).fetchone()
    assert outbox_status == ("failed", "neo4j unavailable")

    retried = service.retry(ingestion.id)

    assert retried.status == "publishing"
    assert repository.projection_summary()["queued"] == 1


def test_projection_heartbeat_prevents_duplicate_short_lease_execution(tmp_path) -> None:
    projector = _SlowProjector(delay=1.4)
    repository, service = _service(tmp_path, projector=projector)
    ingestion = repository.create_ingestion(
        topic="投影续租", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())
    repository.add_candidate_entity(_entity(ingestion, "candidate-slow", "慢投影事实"))
    service.decide("candidate-slow", CandidateDecision(decision="approve"))
    worker = KnowledgeWorker(
        repository,
        ingestion_service=service,
        report_service=KnowledgeReportService(repository, require_live_llm=False),
        lease_seconds=1,
        worker_id="projection-worker",
    )

    thread = threading.Thread(target=worker.run_once)
    thread.start()
    time.sleep(1.1)
    duplicate = repository.claim_projection(lease_seconds=1, owner_id="other-worker")
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert duplicate is None
    assert projector.calls == 1
    assert repository.projection_summary()["completed"] == 1
    heartbeat = repository.list_executor_heartbeats("worker")[0]
    assert heartbeat.id == "projection-worker"
    assert heartbeat.current_job_id is None


def test_stale_projection_attempt_cannot_complete_or_fail_new_owner(tmp_path) -> None:
    repository, service = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic="投影代隔离", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())
    repository.add_candidate_entity(_entity(ingestion, "candidate-fence", "投影代隔离"))
    service.decide("candidate-fence", CandidateDecision(decision="approve"))
    stale = repository.claim_projection(lease_seconds=-1, owner_id="old-worker")
    current = repository.claim_projection(lease_seconds=30, owner_id="new-worker")
    assert stale is not None and current is not None
    assert stale.id == current.id and current.attempts == 2

    with pytest.raises(RuntimeError, match="no longer owned"):
        repository.complete_projection(
            stale.id,
            expected_attempt=stale.attempts,
            expected_owner=stale.lease_owner,
        )
    with pytest.raises(RuntimeError, match="no longer owned"):
        repository.fail_projection(
            stale.id,
            "late failure",
            expected_attempt=stale.attempts,
            expected_owner=stale.lease_owner,
        )

    repository.complete_projection(
        current.id,
        expected_attempt=current.attempts,
        expected_owner=current.lease_owner,
    )
    assert repository.projection_summary()["completed"] == 1


def test_347_candidate_batch_reports_exact_conflicts_and_blocked_relations(tmp_path) -> None:
    repository, service = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic="347 候选验收", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())

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


@pytest.mark.parametrize("candidate_count", [101, 279])
def test_confidence_auto_approval_chunks_candidates_at_the_public_batch_limit(
    tmp_path, candidate_count: int
) -> None:
    repository, service = _service(tmp_path)
    ingestion = repository.create_ingestion(
        topic=f"{candidate_count} 条高置信度候选",
        sources=["paper.pdf"],
        pdf_max_pages=3,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())
    for index in range(candidate_count):
        repository.add_candidate_entity(
            _entity(
                ingestion,
                f"candidate-confident-{index}",
                f"高置信度实体 {index}",
                confidence=0.91,
            )
        )

    result = service.auto_approve_confident(ingestion.id)

    assert result.requested == candidate_count
    assert result.applied == candidate_count
    assert result.replayed == 0
    assert result.skipped == []
    assert result.ingestion.published_count == candidate_count
    assert result.ingestion.status == "publishing"
