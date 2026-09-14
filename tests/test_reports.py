import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.query import KnowledgeQueryService, _latin_query_terms, _rank_evidence
from app.knowledge.report_inputs import report_request
from app.knowledge.report_repository import StaleReportExecution
from app.knowledge.reports import KnowledgeReportService, _evaluate
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, ChunkSearchHit, EvidenceSpan, ReportEvidence
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.llms.provider import MockLLMClient
from app.worker import KnowledgeWorker
from tests.core_fixtures import SQLiteReportQuery, persist_evidence_chunk


def _repository_with_paper(tmp_path):
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    ingestion = repository.create_ingestion(
        topic="正式报告知识", sources=["paper.pdf"], pdf_max_pages=5, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="paper:formal",
        chunk_id="paper:formal:page:2:chunk:0",
        page_start=2,
        page_end=2,
        quote="The approved method improves evidence-grounded research synthesis.",
    )
    candidate = CandidateEntity(
        id="candidate-formal-paper",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.topic_slug,
        name="Formal Evidence Paper",
        type="Paper",
        summary="一篇提供正式、可定位证据并用于报告生成的研究论文。",
        confidence=0.95,
        evidence=evidence,
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    repository.add_candidate_entity(candidate)
    repository.publish_entity(candidate.id)
    return repository, ingestion


class _Query(SQLiteReportQuery):
    def __init__(self, repository):
        # Both report citations must have real SQLite source/chunk prerequisites.
        papers = repository.list_published_entities()
        if papers:
            ingestion = repository.list_ingestions()[0]
            persist_evidence_chunk(
                repository, ingestion_id=ingestion.id,
                evidence=EvidenceSpan(
                    paper_id="paper:formal", chunk_id="paper:formal:page:3:chunk:0",
                    page_start=3, page_end=3,
                    quote="A critic checks citation fidelity before publication.",
                ),
            )
        super().__init__(repository)


class _RevisingLLM:
    provider_name = "openai"

    def __init__(self):
        self.calls = 0

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls += 1
        if self.calls == 1:
            return "# 初稿\n\n只有一条证据。[E1]"
        return (
            "# 研究报告\n\n## 背景\n正式知识支持证据约束写作。[E1]\n\n"
            "## 评估\n引用忠实度在发布前接受检查。[E2]\n\n"
            "## 结论\n报告结论可回溯到原文页码。[E1][E2]"
        )


class _UngroundedLLM:
    provider_name = "openai"

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        return "# 报告\n\n没有合法引用的结论。"


class _SlowGroundedLLM:
    provider_name = "openai"

    def __init__(self):
        self.calls = 0
        self.started = Event()

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls += 1
        self.started.set()
        time.sleep(1.4)
        return (
            "# 研究报告\n\n## 背景\n正式知识支持证据约束写作。[E1]\n\n"
            "## 评估\n引用忠实度在发布前接受检查。[E2]\n\n"
            "## 结论\n报告结论可回溯到原文页码。[E1][E2]"
        )


def _report_stack(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "vault"),
    )
    llm = _RevisingLLM()
    reports = KnowledgeReportService(
        repository,
        query_service=_Query(repository),
        llm=llm,
        settings=settings,
        require_live_llm=True,
    )
    ingestion_service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        require_live_llm=False,
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=reports,
        lease_seconds=30,
    )
    return repository, ingestion, reports, worker, llm


def _wait_for_report_status(
    repository: KnowledgeRepository,
    report_id: str,
    status: str,
    timeout: float = 4.0,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = repository.reports.get_report(report_id)
        if report.status == status:
            return report
        time.sleep(0.02)
    raise AssertionError(f"report {report_id} did not reach {status}")


def test_report_worker_revises_once_and_persists_evidence_evaluation(tmp_path) -> None:
    repository, ingestion, reports, worker, llm = _report_stack(tmp_path)
    submitted = reports.submit(
        query="如何生成有据可查的研究报告？",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
        report_depth="standard",
    )

    assert submitted.status == "queued"
    assert worker.run_once()
    completed = repository.reports.get_report(submitted.id)

    assert completed.status == "completed"
    assert llm.calls == 2
    assert completed.evaluation is not None
    assert completed.evaluation.passed
    assert completed.evaluation.revision_applied
    assert completed.evaluation.evidence_grounding == 1.0
    assert completed.evaluation.citation_coverage == 1.0
    assert completed.evaluation.citation_fidelity == 1.0
    assert len(completed.evidence) == 2
    assert completed.run_metadata["prompt_version"] == "knowledge-report-v3"
    assert completed.run_metadata["generation_calls"] == 2
    assert completed.run_metadata["latency_ms"] >= 0
    assert repository.jobs.job_summary()["completed"] == 1


def test_report_prompts_include_quality_contract_and_revision_evidence(tmp_path) -> None:
    repository, ingestion, reports, _, _ = _report_stack(tmp_path)
    report = reports.submit(
        query="如何生成有据可查的研究报告？",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    evidence = [
        ReportEvidence.model_validate(item)
        for item in reports.query_service.search(report.query, top_k=2)["evidence"]
    ]

    initial = reports._prompt(report, evidence, [])
    revision = reports._revision_prompt("# 缺少结构的初稿\n\n只有一个结论。[E1]", evidence)

    assert "至少三个以 `##` 开头" in initial
    assert "每条正式正文证据至少引用一次" in initial
    assert "至少三个以 `##` 开头" in revision
    for item in evidence:
        assert item.title in revision
        assert item.text in revision
        assert f"[{item.id}]" in revision


def test_targeted_report_claim_does_not_consume_older_queued_work(tmp_path) -> None:
    repository, ingestion, reports, _, _ = _report_stack(tmp_path)
    older_ingestion = repository.create_ingestion(
        topic="更早的入库任务",
        sources=["older.pdf"],
        pdf_max_pages=3,
        enqueue=True,
    )
    report = reports.submit(
        query="只执行指定报告任务",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )

    claimed = repository.jobs.claim_resource_job("report", report.id, lease_seconds=30)

    assert claimed is not None
    assert claimed.kind == "report"
    assert claimed.resource_id == report.id
    assert repository.jobs.get_resource_job("ingestion", older_ingestion.id).status == "queued"
    assert repository.jobs.get_resource_job("report", report.id).attempts == 1


def test_targeted_claim_is_idempotent_until_its_lease_expires(tmp_path) -> None:
    repository, ingestion, reports, _, _ = _report_stack(tmp_path)
    report = reports.submit(
        query="重复点击不得重复执行",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )

    first = repository.jobs.claim_resource_job("report", report.id, lease_seconds=30)
    duplicate = repository.jobs.claim_resource_job("report", report.id, lease_seconds=30)

    assert first is not None
    assert duplicate is None
    assert repository.jobs.get_resource_job("report", report.id).attempts == 1

    with repository._connect() as connection:
        connection.execute(
            "UPDATE knowledge_jobs SET lease_until = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", first.id),
        )
    reclaimed = repository.jobs.claim_resource_job("report", report.id, lease_seconds=30)

    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.attempts == 2


def test_concurrent_targeted_claims_have_a_single_winner(tmp_path) -> None:
    repository, ingestion, reports, _, _ = _report_stack(tmp_path)
    report = reports.submit(
        query="并发点击只允许一次执行",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return repository.jobs.claim_resource_job("report", report.id, lease_seconds=30)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))

    assert sum(result is not None for result in results) == 1
    assert repository.jobs.get_resource_job("report", report.id).attempts == 1


def test_failed_report_job_can_be_cleanly_retried_with_the_same_identity(tmp_path) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    service = KnowledgeReportService(
        repository,
        query_service=_Query(repository),
        llm=_UngroundedLLM(),
        require_live_llm=True,
    )
    submitted = service.submit(
        query="失败后原位重试报告",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    first_job = repository.jobs.claim_resource_job("report", submitted.id, lease_seconds=30)

    assert first_job is not None
    failed = service.execute_claimed(submitted.id, first_job.id, first_job.attempts)
    assert failed.status == "failed"
    assert failed.content
    assert failed.evidence
    assert repository.jobs.get_resource_job("report", submitted.id).status == "failed"

    reset = repository.reports.reset_report_for_retry(submitted.id)

    assert reset.id == submitted.id
    assert reset.status == "queued"
    assert reset.content == ""
    assert reset.evidence == []
    assert reset.evaluation is None
    assert reset.error is None
    assert repository.jobs.get_resource_job("report", submitted.id).status == "queued"

    service.llm = _RevisingLLM()
    second_job = repository.jobs.claim_resource_job("report", submitted.id, lease_seconds=30)
    assert second_job is not None
    completed = service.execute_claimed(submitted.id, second_job.id, second_job.attempts)

    assert completed.status == "completed"
    assert second_job.attempts == 2
    assert repository.jobs.get_resource_job("report", submitted.id).status == "completed"


def test_expired_dispatched_report_is_recovered_after_restart(tmp_path) -> None:
    repository, ingestion, reports, _, _ = _report_stack(tmp_path)
    submitted = reports.submit(
        query="服务重启后继续生成报告",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
        auto_execute=True,
    )
    abandoned = repository.reports.claim_dispatched_report_job(lease_seconds=-1)

    assert abandoned is not None
    assert repository.reports.get_report(submitted.id).status == "running"

    restarted = KnowledgeRepository(repository.path)
    restarted.recover_running_work()
    assert restarted.jobs.get_resource_job("report", submitted.id).status == "queued"
    settings = Settings(
        knowledge_db_path=restarted.path,
        knowledge_vault_path=str(tmp_path / "restarted-vault"),
    )
    restarted_service = KnowledgeReportService(
        restarted,
        query_service=_Query(repository),
        llm=_RevisingLLM(),
        settings=settings,
        require_live_llm=True,
    )
    ingestion_service = KnowledgeIngestionService(
        restarted,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        require_live_llm=False,
    )
    worker = KnowledgeWorker(
        restarted,
        ingestion_service=ingestion_service,
        report_service=restarted_service,
        lease_seconds=30,
    )
    assert worker.run_once()
    completed = restarted.reports.get_report(submitted.id)

    assert completed.status == "completed"
    recovered_job = restarted.jobs.get_resource_job("report", submitted.id)
    assert recovered_job.status == "completed"
    assert recovered_job.attempts == 2


def test_report_lease_heartbeat_prevents_duplicate_long_execution(tmp_path) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    llm = _SlowGroundedLLM()
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "heartbeat-vault"),
    )
    service = KnowledgeReportService(
        repository,
        query_service=_Query(repository),
        llm=llm,
        settings=settings,
        require_live_llm=True,
    )
    submitted = service.submit(
        query="长时间生成时续租避免重复调用",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
        auto_execute=True,
    )
    ingestion_service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        require_live_llm=False,
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=service,
        lease_seconds=1,
    )
    worker_thread = Thread(target=worker.run_once)
    worker_thread.start()
    assert llm.started.wait(timeout=2.0)
    initial_job = repository.jobs.get_resource_job("report", submitted.id)
    initial_lease = initial_job.lease_until
    time.sleep(1.05)
    renewed_job = repository.jobs.get_resource_job("report", submitted.id)
    duplicate = repository.reports.claim_dispatched_report_job(lease_seconds=1)
    worker_thread.join(timeout=3.0)
    completed = repository.reports.get_report(submitted.id)

    assert initial_lease is not None
    assert renewed_job.lease_until is not None
    assert renewed_job.lease_until > initial_lease
    assert renewed_job.attempts == 1
    assert duplicate is None
    assert completed.status == "completed"
    assert llm.calls == 1
    assert not worker_thread.is_alive()


def test_two_workers_do_not_duplicate_slow_report_llm_calls(tmp_path) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    llm = _SlowGroundedLLM()
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "two-workers-vault"),
    )
    service = KnowledgeReportService(
        repository,
        query_service=_Query(repository),
        llm=llm,
        settings=settings,
        require_live_llm=True,
    )
    ingestion_service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        require_live_llm=False,
    )
    first_worker = KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=service,
        lease_seconds=1,
        worker_id="worker-alpha",
    )
    second_worker = KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=service,
        lease_seconds=1,
        worker_id="worker-beta",
    )
    submitted = service.submit(
        query="双 Worker 不重复执行同一慢报告",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
        auto_execute=True,
    )
    worker_thread = Thread(target=first_worker.run_once)
    worker_thread.start()
    assert llm.started.wait(timeout=2.0)
    second_worker.run_once()
    completed = _wait_for_report_status(repository, submitted.id, "completed")
    worker_thread.join(timeout=2.0)

    job = repository.jobs.get_resource_job("report", submitted.id)
    assert completed.status == "completed"
    assert job.status == "completed"
    assert job.attempts == 1
    assert llm.calls == 1
    assert not worker_thread.is_alive()


def test_stale_report_attempt_cannot_overwrite_new_owner(tmp_path) -> None:
    repository, ingestion, reports, _, _ = _report_stack(tmp_path)
    submitted = reports.submit(
        query="旧执行不得覆盖新的报告执行",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
        auto_execute=True,
    )
    old = repository.reports.claim_dispatched_report_job(lease_seconds=-1)
    new = repository.reports.claim_dispatched_report_job(lease_seconds=30)

    assert old is not None
    assert new is not None
    assert new.attempts == old.attempts + 1
    with pytest.raises(StaleReportExecution):
        repository.reports.update_report(
            submitted.id,
            status="failed",
            error="stale stage write",
            expected_job_attempt=old.attempts,
        )
    with pytest.raises(StaleReportExecution):
        repository.reports.finalize_report_execution(
            submitted.id,
            old.id,
            expected_attempt=old.attempts,
            status="failed",
            error="stale terminal write",
        )
    assert not repository.jobs.complete_job(old.id, expected_attempt=old.attempts)
    assert not repository.jobs.fail_job(old.id, "stale failure", expected_attempt=old.attempts)

    running = repository.jobs.get_resource_job("report", submitted.id)
    assert running.status == "running"
    assert running.attempts == new.attempts
    result = reports.query_service.search(submitted.query, topic_slugs=submitted.topic_slugs)
    completed = repository.reports.finalize_report_execution(
        submitted.id,
        new.id,
        expected_attempt=new.attempts,
        status="completed",
        content="# 新执行结果",
        expected_request=report_request(submitted),
        read_guard=result["retrieval_diagnostics"]["report_input"],
        run_metadata={"current_stage": "completed"},
    )

    assert completed.status == "completed"
    assert completed.content == "# 新执行结果"
    assert repository.jobs.get_resource_job("report", submitted.id).status == "completed"


def test_reports_never_fall_back_to_mock_or_unpublished_evidence(tmp_path) -> None:
    empty = KnowledgeRepository(str(tmp_path / "empty.db"))
    with pytest.raises(LiveLLMRequiredError):
        KnowledgeReportService(empty, llm=MockLLMClient()).submit(query="test")

    service = KnowledgeReportService(
        empty,
        query_service=_Query(empty),
        llm=_RevisingLLM(),
        require_live_llm=True,
    )
    with pytest.raises(ValueError, match="没有已审核"):
        service.submit(query="test")


def test_claimed_report_rechecks_live_llm_before_query_or_generation(tmp_path) -> None:
    repository, ingestion, service, _, _ = _report_stack(tmp_path)
    submitted = service.submit(
        query="恢复任务仍需真实模型",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    job = repository.jobs.claim_resource_job(
        "report",
        submitted.id,
        lease_seconds=30,
        owner_id="worker-live-gate",
    )
    assert job is not None
    service.llm = MockLLMClient()

    failed = service.execute_claimed(
        submitted.id,
        job.id,
        job.attempts,
        expected_owner=job.lease_owner,
    )

    assert failed.status == "failed"
    assert "真实 LLM" in (failed.error or "")
    assert repository.jobs.get_resource_job("report", submitted.id).status == "failed"


def test_report_fails_after_the_single_revision_budget_is_exhausted(tmp_path) -> None:
    repository, _ = _repository_with_paper(tmp_path)
    service = KnowledgeReportService(
        repository,
        query_service=_Query(repository),
        llm=_UngroundedLLM(),
        require_live_llm=True,
    )
    submitted = service.submit(query="无法通过质量门的报告", top_k=2)

    failed = service.run(submitted.id)

    assert failed.status == "failed"
    assert failed.evaluation is not None
    assert failed.evaluation.revision_applied
    assert not failed.evaluation.passed
    assert "一次修订" in (failed.error or "")


def test_report_quality_gate_requires_three_sources_when_three_are_relevant() -> None:
    evidence = [
        ReportEvidence(
            id=f"E{index}",
            paper_id=f"paper:{index}",
            chunk_id=f"chunk:{index}",
            title=f"Paper {index}",
            text="Relevant grounded evidence for a research report.",
            page_start=1,
            page_end=1,
            score=0.9,
        )
        for index in (1, 2)
    ]
    evaluation = _evaluate(
        "# 报告\n\n## 背景\n证据。[E1]\n\n## 分析\n证据。[E2]\n\n## 结论\n[E1][E2]",
        evidence,
        retrieval_diagnostics={
            "relevant_paper_count": 3,
            "required_source_count": 3,
            "retrieval_relevance": 1.0,
        },
        revision_applied=False,
    )

    assert evaluation.retrieval_relevance == 1.0
    assert evaluation.source_diversity == pytest.approx(0.6667)
    assert evaluation.selected_source_count == 2
    assert evaluation.available_relevant_source_count == 3
    assert not evaluation.passed


class _ChunkSearch:
    def __init__(self):
        self.allowed = set()

    def search(self, query, *, allowed_paper_ids, top_k):
        self.allowed = allowed_paper_ids
        return []


class _GraphSearch:
    def search(self, query, *, topic_slugs, limit=20):
        return []


class _EvidenceChunkSearch:
    def search(self, query, *, allowed_paper_ids, top_k):
        return [
            ReportEvidence(
                id="E-reference",
                paper_id="paper:formal",
                chunk_id="reference",
                title="Formal Evidence Paper",
                text="References [1] Agent systems and related work.",
                page_start=20,
                page_end=20,
                score=0.99,
            ),
            ReportEvidence(
                id="E-main",
                paper_id="paper:formal",
                chunk_id="main",
                title="Formal Evidence Paper",
                text="An agent uses verified evidence to plan a research task.",
                page_start=2,
                page_end=2,
                score=0.32,
            ),
            ReportEvidence(
                id="E-second",
                paper_id="paper:formal",
                chunk_id="second",
                title="Formal Evidence Paper",
                text="The agent checks evidence coverage before publication.",
                page_start=3,
                page_end=3,
                score=0.28,
            ),
            ReportEvidence(
                id="E-third",
                paper_id="paper:formal",
                chunk_id="third",
                title="Formal Evidence Paper",
                text="Agent runs retain a reviewable execution trace.",
                page_start=4,
                page_end=4,
                score=0.27,
            ),
            ReportEvidence(
                id="E-other",
                paper_id="paper:other",
                chunk_id="other",
                title="Other Paper",
                text="Another agent evaluates source-grounded answers.",
                page_start=1,
                page_end=1,
                score=0.3,
            ),
        ]


class _SQLiteCandidateSearch:
    def search(self, query, *, allowed_paper_ids, top_k):
        return [
            ChunkSearchHit(
                chunk_id="paper:formal:page:2:chunk:0",
                score=0.8,
            ),
            ChunkSearchHit(chunk_id="untrusted-or-missing-chunk", score=1.0),
        ]


class _UnavailableGraphSearch:
    def search(self, query, *, topic_slugs, limit=20):
        raise RuntimeError("neo4j is unavailable")


def test_query_service_filters_chunks_by_sqlite_published_paper_ids(tmp_path) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    chunks = _ChunkSearch()
    query = KnowledgeQueryService(
        repository,
        chunk_search=chunks,
        graph_search=_GraphSearch(),
    )

    query.search("grounded", topic_slugs=[ingestion.topic_slug], top_k=5)

    assert chunks.allowed == {"paper:formal"}


def test_query_service_rehydrates_vector_ids_from_sqlite_and_rejects_unknown_ids(
    tmp_path,
) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    query = KnowledgeQueryService(
        repository,
        chunk_search=_SQLiteCandidateSearch(),
        graph_search=_GraphSearch(),
    )

    result = query.search("grounded", topic_slugs=[ingestion.topic_slug], top_k=3)

    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["chunk_id"] == "paper:formal:page:2:chunk:0"
    assert result["evidence"][0]["text"] == (
        "The approved method improves evidence-grounded research synthesis."
    )
    assert result["evidence"][0]["title"] == "Core Evidence Fixture"


def test_ranking_filters_bibliography_diversifies_and_graph_degrades(tmp_path) -> None:
    ranked = _rank_evidence(
        "agent",
        _EvidenceChunkSearch().search("agent", allowed_paper_ids=set(), top_k=3),
        top_k=3,
    )
    assert len(ranked) == 3
    assert all(item.chunk_id != "reference" for item in ranked)
    assert sum(item.paper_id == "paper:formal" for item in ranked) == 2
    assert any(item.paper_id == "paper:other" for item in ranked)

    repository, ingestion = _repository_with_paper(tmp_path)
    query = KnowledgeQueryService(
        repository,
        chunk_search=_ChunkSearch(),
        graph_search=_UnavailableGraphSearch(),
    )
    result = query.search("agent", topic_slugs=[ingestion.topic_slug], top_k=3)

    assert result["evidence"] == []
    assert result["graph"] == []
    assert result["warnings"] == ["图关系服务暂时不可用，当前仅展示已定位的原文证据。"]


def test_hash_query_keeps_bilingual_technical_anchors() -> None:
    assert _latin_query_terms("Toolformer 如何决定调用哪个 API？") == "toolformer api"
    assert _latin_query_terms("纯中文问题") == ""


def test_report_api_exposes_content_evidence_and_download(tmp_path) -> None:
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=worker.ingestion_service,
            report_service=reports,
        )
    ) as client:
        submitted = client.post(
            "/api/reports",
            json={
                "query": "如何生成有据可查的研究报告？",
                "topic_slugs": [ingestion.topic_slug],
                "top_k": 2,
                "report_depth": "standard",
            },
        )
        report_id = submitted.json()["id"]
        assert worker.run_once()
        detail = client.get(f"/api/reports/{report_id}")
        evidence = client.get(f"/api/reports/{report_id}/evidence")
        download = client.get(f"/api/reports/{report_id}/download")

    assert submitted.status_code == 202
    assert detail.json()["status"] == "completed"
    assert len(evidence.json()["evidence"]) == 2
    assert download.status_code == 200
    assert "研究报告" in download.text
    assert download.headers["content-type"].startswith("text/markdown")


def test_report_api_prioritizes_the_selected_report_for_the_worker(tmp_path) -> None:
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)
    first = reports.submit(
        query="保留在队列中的第一份报告",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    second = reports.submit(
        query="由工作台直接执行的第二份报告",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=worker.ingestion_service,
            report_service=reports,
        )
    ) as client:
        executed = client.post(f"/api/reports/{second.id}/execute")
        assert worker.run_once()
        _wait_for_report_status(repository, second.id, "completed")
        repeated = client.post(f"/api/reports/{second.id}/execute")

    assert executed.status_code == 202
    assert repository.reports.get_report(second.id).status == "completed"
    assert repository.jobs.get_resource_job("report", second.id).attempts == 1
    assert repository.reports.get_report(first.id).status == "queued"
    assert repository.jobs.get_resource_job("report", first.id).status == "queued"
    assert repeated.status_code == 409


def test_report_api_creates_and_queues_in_one_request(tmp_path) -> None:
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)
    application = create_app(
        knowledge_repository=repository,
        knowledge_service=worker.ingestion_service,
        report_service=reports,
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/reports/execute",
            json={
                "query": "一次请求创建并开始研究报告",
                "collection_slugs": [ingestion.topic_slug],
                "top_k": 2,
                "report_depth": "standard",
            },
        )
        report_id = response.json()["id"]
        assert response.json()["status"] == "queued"
        assert worker.run_once()
        completed = _wait_for_report_status(repository, report_id, "completed")

    assert response.status_code == 202
    assert completed.status == "completed"
    job = repository.jobs.get_resource_job("report", report_id)
    assert job.payload["auto_execute"] is True
    assert job.status == "completed"
    assert job.attempts == 1


def test_report_api_retries_a_failed_report_in_place(tmp_path) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "vault"),
    )
    reports = KnowledgeReportService(
        repository,
        query_service=_Query(repository),
        llm=_UngroundedLLM(),
        settings=settings,
        require_live_llm=True,
    )
    ingestion_service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        require_live_llm=False,
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=reports,
        lease_seconds=30,
    )
    submitted = reports.submit(
        query="工作台失败报告重试",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
    )
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=ingestion_service,
            report_service=reports,
        )
    ) as client:
        first = client.post(f"/api/reports/{submitted.id}/execute")
        assert worker.run_once()
        _wait_for_report_status(repository, submitted.id, "failed")
        reports.llm = _RevisingLLM()
        retried = client.post(f"/api/reports/{submitted.id}/retry")
        assert worker.run_once()
        _wait_for_report_status(repository, submitted.id, "completed")

    assert first.status_code == 202
    assert retried.status_code == 202
    assert repository.reports.get_report(submitted.id).status == "completed"
    job = repository.jobs.get_resource_job("report", submitted.id)
    assert job.status == "completed"
    assert job.attempts == 2
