from __future__ import annotations

import sqlite3

import pytest

from app.knowledge.query import KnowledgeQueryService
from app.knowledge.report_inputs import report_request
from app.knowledge.report_repository import StaleReportExecution
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from tests.test_query_consistency import _graph_setup, _Search, _vector, _write


class _LLM:
    provider_name = "fixture"

    def __init__(self, callback=lambda: None):
        self.callback, self.calls = callback, 0

    def invoke(self, *args, **kwargs):
        self.calls += 1
        self.callback()
        return "## 背景\n支持结论。[E1]\n## 比较\n有原文证据。[E1]\n## 局限\n限于证据。[E1]"


def test_report_creation_and_queue_roll_back_together(tmp_path, monkeypatch):
    repository = KnowledgeRepository(str(tmp_path / "reports.db"))
    enqueue = repository.jobs.enqueue_job_tx

    def fail_after_enqueue(*args, **kwargs):
        enqueue(*args, **kwargs)
        raise RuntimeError("queue failure after insert")

    request = dict(
        report_id="atomic-report", query="evidence", topic_slugs=["inbox"],
        top_k=5, report_depth="standard",
    )
    with monkeypatch.context() as patch:
        patch.setattr(repository.jobs, "enqueue_job_tx", fail_after_enqueue)
        with pytest.raises(RuntimeError, match="queue failure"):
            repository.reports.create_report(**request)

    assert repository.reports.list_reports() == []
    assert repository.jobs.job_summary() == {}
    report = repository.reports.create_report(**request)
    restarted = KnowledgeRepository(repository.path)
    assert restarted.reports.get_report(report.id) == report
    assert restarted.jobs.get_resource_job("report", report.id).status == "queued"


def _stack(tmp_path):
    repository, ingestion = _graph_setup(tmp_path)
    query = KnowledgeQueryService(
        repository, chunk_search=_vector(),
        graph_search=_Search(lambda *_: [{"edge_id": "query-edge"}]),
    )
    llm = _LLM()
    service = KnowledgeReportService(repository, query_service=query, llm=llm)
    report = service.submit(query="evidence", topic_slugs=[ingestion.topic_slug])
    return repository, service, report, llm


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE entities SET status = 'withdrawn' WHERE entity_type = 'Paper'", "knowledge"),
    ("UPDATE claims SET status = 'withdrawn'", "knowledge"),
    ("DELETE FROM collection_memberships", "knowledge"),
    ("UPDATE sources SET version = 'new-version'", "knowledge"),
    ("UPDATE documents SET title = 'Changed title'", "knowledge"),
    ("UPDATE chunks SET content = 'tampered body'", "knowledge"),
    ("UPDATE documents SET content_status = 'unavailable'", "knowledge"),
    ("UPDATE relations SET status = 'withdrawn'", "knowledge"),
    ("UPDATE entities SET name = 'Changed method' WHERE entity_type = 'Method'", "knowledge"),
    ("UPDATE reports SET query = 'different question'", "request"),
    ("UPDATE reports SET topic_slugs_json = '[]'", "request"),
    ("UPDATE reports SET top_k = top_k + 1", "request"),
    ("UPDATE reports SET report_depth = 'deep'", "request"),
])
def test_generation_changes_fail_atomically_with_input_diagnostics(tmp_path, sql, reason):
    repository, service, report, llm = _stack(tmp_path)
    llm.callback = lambda: _write(repository, sql)
    failed = service.run(report.id)
    assert failed.status == "failed", failed.error
    assert failed.content == ""
    assert repository.jobs.get_resource_job("report", report.id).status == "failed"
    check = failed.run_metadata["submission_validation"]
    assert check["outcome"] == f"report_{reason}_changed"
    assert check["request"]["query"] == "evidence"
    assert len(check["input_fingerprint"]) == 64
    assert failed.evaluation is not None and not failed.evaluation.passed
    assert failed.evidence and llm.calls == 1


def test_stable_report_completes_and_direct_rerun_is_idempotent(tmp_path):
    repository, service, report, llm = _stack(tmp_path)
    completed = service.run(report.id)
    assert completed.status == "completed", completed.error
    assert completed.run_metadata["submission_validation"]["outcome"] == "verified"
    assert service.run(report.id) == completed
    assert repository.jobs.get_resource_job("report", report.id).attempts == 1
    assert llm.calls == 1


def test_new_claim_during_generation_fences_old_stage_and_terminal_writes(tmp_path):
    repository, service, report, llm = _stack(tmp_path)
    old = repository.jobs.claim_resource_job("report", report.id, owner_id="old")

    def replace_claim():
        _write(repository, "UPDATE knowledge_jobs SET lease_until = '2000-01-01'")
        new = repository.jobs.claim_resource_job("report", report.id, owner_id="new")
        assert new.attempts == old.attempts + 1

    llm.callback = replace_claim
    current = service.execute_claimed(report.id, old.id, old.attempts, expected_owner="old")
    assert current.status == "running" and current.content == ""
    assert repository.jobs.get_resource_job("report", report.id).lease_owner == "new"


@pytest.mark.parametrize("wrong", ["job", "owner"])
def test_wrong_claim_identity_stops_before_retrieval(tmp_path, wrong):
    repository, service, report, llm = _stack(tmp_path)
    job = repository.jobs.claim_resource_job("report", report.id, owner_id="owner")
    with pytest.raises(StaleReportExecution):
        service.run(
            report.id, expected_job_id="other" if wrong == "job" else job.id,
            expected_job_attempt=job.attempts,
            expected_job_owner="other" if wrong == "owner" else "owner",
        )
    assert llm.calls == 0
    assert repository.reports.get_report(report.id).status == "queued"


def test_terminal_report_changed_during_generation_is_not_reopened(tmp_path):
    repository, service, report, llm = _stack(tmp_path)
    llm.callback = lambda: _write(
        repository, "UPDATE reports SET status = 'failed', error = 'externally closed'",
    )
    with pytest.raises(StaleReportExecution):
        service.run(report.id)
    assert repository.reports.get_report(report.id).error == "externally closed"


def test_input_validation_and_terminal_writes_share_write_lock(tmp_path, monkeypatch):
    import app.knowledge.report_inputs as inputs

    repository, service, report, llm = _stack(tmp_path)
    original = inputs.validate_read_guard_tx
    observations = []
    reads = []
    original_scope = repository.core_repository.authorized_report_paper_ids_tx

    def scope(connection, topics):
        reads.append(connection)
        return original_scope(connection, topics)

    def generation():
        assert reads
        for connection in reads:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")

    monkeypatch.setattr(repository.core_repository, "authorized_report_paper_ids_tx", scope)
    llm.callback = generation

    def validate(connection, *args):
        assert connection.in_transaction
        with sqlite3.connect(repository.path, timeout=0) as writer:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.execute("UPDATE sources SET version = 'racing-writer'")
        observations.append(connection)
        return original(connection, *args)

    monkeypatch.setattr(inputs, "validate_read_guard_tx", validate)
    assert service.run(report.id).status == "completed"
    assert len(observations) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observations[0].execute("SELECT 1")


def test_terminal_job_failure_rolls_back_report_update(tmp_path):
    repository, service, report, _ = _stack(tmp_path)
    result = service.query_service.search(report.query, topic_slugs=report.topic_slugs)
    job = repository.jobs.claim_resource_job("report", report.id)
    _write(repository, """
        CREATE TRIGGER reject_report_job BEFORE UPDATE OF status ON knowledge_jobs
        WHEN NEW.status = 'completed' BEGIN SELECT RAISE(ABORT, 'injected job failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="injected job failure"):
        repository.reports.finalize_report_execution(
            report.id, job.id, expected_attempt=job.attempts, expected_owner=job.lease_owner,
            status="completed", content="must roll back", expected_request=report_request(report),
            read_guard=result["retrieval_diagnostics"]["report_input"],
        )
    assert repository.reports.get_report(report.id) == report
    assert repository.jobs.get_resource_job("report", report.id).status == "running"


def test_missing_input_guard_fails_before_model_call(tmp_path):
    repository, service, report, llm = _stack(tmp_path)
    service.query_service = _Search(lambda *_: {"evidence": []})
    failed = service.run(report.id)
    assert failed.status == "failed" and "输入指纹" in failed.error
    assert llm.calls == 0
    assert repository.jobs.get_resource_job("report", report.id).status == "failed"


def test_repository_completion_cannot_bypass_input_check(tmp_path):
    repository, _, report, _ = _stack(tmp_path)
    job = repository.jobs.claim_resource_job("report", report.id)
    with pytest.raises(ValueError, match="original request and input guard"):
        repository.reports.finalize_report_execution(
            report.id, job.id, expected_attempt=job.attempts, status="completed", content="bad",
        )
    assert repository.reports.get_report(report.id) == report
