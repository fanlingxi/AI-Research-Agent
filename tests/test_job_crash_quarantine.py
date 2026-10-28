import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.agent.errors import AgentRunConflictError
from app.config.settings import Settings
from app.execution import ExecutionFence
from app.knowledge.report_repository import StaleReportExecution
from app.knowledge.repository import KnowledgeRepository, StaleIngestionExecution
from app.research_commands.repository import ResearchCommandRepository
from app.runtime.job_repository import CRASH_RECOVERY_ERROR_CODE, CRASH_RECOVERY_ERROR_MESSAGE
from tests.test_agent_runtime import _foundation_request, _runtime_stack
from tests.test_reliability import _claimed_ingestion_service, _SlowExtractor
from tests.test_reports import _report_stack


@pytest.mark.parametrize("kind", ["ingestion", "report"])
@pytest.mark.parametrize("path", ["worker", "resource", "startup", "dispatch"])
def test_all_recovery_paths_quarantine_and_allow_manual_retry(tmp_path, kind, path):
    repo = KnowledgeRepository(str(tmp_path / "queue.db"))
    if kind == "ingestion":
        resource = repo.create_ingestion(
            sources=["poison.pdf"], pdf_max_pages=3, auto_execute=True
        )
        dispatch = repo.claim_dispatched_ingestion_job
        get_resource = repo.get_ingestion
        retry = repo.reset_ingestion
        terminal = "interrupted"
    else:
        resource = repo.reports.create_report(
            query="question", topic_slugs=[], top_k=3, report_depth="brief", auto_execute=True
        )
        dispatch = repo.reports.claim_dispatched_report_job
        get_resource = repo.reports.get_report
        retry = repo.reports.reset_report_for_retry
        terminal = "failed"

    def claim():
        if path == "worker":
            return repo.jobs.claim_job(lease_seconds=-1, owner_id="same-worker")
        if path == "dispatch":
            return dispatch(lease_seconds=-1, owner_id="same-worker")
        if path == "startup":
            repo.recover_running_work()
            # Recovery is idempotent without a new execution/expired lease.
            repo.recover_running_work()
        return repo.jobs.claim_resource_job(
            kind, resource.id, lease_seconds=-1, owner_id="same-worker"
        )

    old = None
    for attempt in range(1, 4):
        current = claim()
        assert current is not None
        assert current.attempts == attempt
        assert current.crash_recoveries == attempt - 1
        old = current
    assert claim() is None
    assert claim() is None
    quarantined = repo.jobs.get_job(old.id)
    assert quarantined.status == "failed"
    assert quarantined.crash_recoveries == 3
    assert quarantined.attempts == 3
    assert quarantined.lease_owner is None and quarantined.lease_until is None
    assert quarantined.last_error == CRASH_RECOVERY_ERROR_MESSAGE
    assert get_resource(resource.id).status == terminal
    assert get_resource(resource.id).error == CRASH_RECOVERY_ERROR_MESSAGE
    assert not repo.jobs.complete_job(
        old.id, expected_attempt=old.attempts, expected_owner=old.lease_owner
    )
    assert not repo.jobs.fail_job(
        old.id, "stale failure", expected_attempt=old.attempts, expected_owner=old.lease_owner
    )
    assert not repo.jobs.renew_job_lease(
        old.id, expected_attempt=old.attempts, expected_owner=old.lease_owner, lease_seconds=30
    )

    assert retry(resource.id, auto_execute=True).status == "queued"
    assert repo.jobs.get_job(old.id).crash_recoveries == 0
    retried = claim()
    assert retried is not None and retried.attempts == 4
    # Even reuse of an owner identifier cannot revive a stale fencing token.
    assert not repo.jobs.complete_job(
        old.id, expected_attempt=old.attempts, expected_owner=old.lease_owner
    )
    assert repo.jobs.complete_job(
        retried.id, expected_attempt=retried.attempts, expected_owner=retried.lease_owner
    )
    assert repo.jobs.get_job(old.id).crash_recoveries == 0


def test_configured_limit_null_lease_active_lease_and_queue_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_JOB_MAX_CRASH_RECOVERIES", "1")
    settings = Settings(_env_file=None)
    assert settings.knowledge_job_max_crash_recoveries == 1
    monkeypatch.setattr(
        "app.runtime.job_repository.get_settings", lambda: settings
    )
    # Also check the normal loader's declared default independently of cache/env.
    assert Settings.model_fields["knowledge_job_max_crash_recoveries"].default == 3
    with pytest.raises(ValidationError):
        Settings(knowledge_job_max_crash_recoveries=0)
    repo = KnowledgeRepository(str(tmp_path / "queue.db"))
    poison = repo.jobs.enqueue_job(kind="ingestion", resource_id="poison", priority=100)
    healthy = repo.jobs.enqueue_job(kind="report", resource_id="healthy")
    claimed = repo.jobs.claim_job(lease_seconds=60)
    assert claimed.id == poison.id
    repo.recover_running_work()
    assert repo.jobs.get_job(poison.id).crash_recoveries == 0
    with repo._connect() as connection:
        connection.execute(
            "UPDATE knowledge_jobs SET lease_until = NULL WHERE id = ?", (poison.id,)
        )
    next_job = repo.jobs.claim_job()
    assert next_job.id == healthy.id
    assert repo.jobs.get_job(poison.id).status == "failed"
    assert next_job.crash_recoveries == 0


def test_ingestion_executes_after_manual_retry_and_rejects_stale_result(tmp_path):
    repo, service, ingestion = _claimed_ingestion_service(tmp_path, _SlowExtractor())
    repo.jobs.max_crash_recoveries = 1
    old = repo.jobs.claim_resource_job("ingestion", ingestion.id, lease_seconds=-1)
    assert repo.jobs.claim_resource_job("ingestion", ingestion.id) is None
    assert service.retry(ingestion.id).status == "queued"
    current = repo.jobs.claim_resource_job("ingestion", ingestion.id)
    with pytest.raises(StaleIngestionExecution):
        repo.finalize_ingestion_execution(
            ingestion.id, old.id, expected_attempt=old.attempts,
            expected_owner=old.lease_owner, status="failed", error="stale",
        )
    assert service.execute_claimed(current).status == "needs_review"
    assert repo.jobs.get_job(current.id).status == "completed"


def test_report_executes_after_manual_retry(tmp_path):
    repo, ingestion, reports, worker, _ = _report_stack(tmp_path)
    repo.jobs.max_crash_recoveries = 1
    report = reports.submit(
        query="如何生成有据可查的报告？", topic_slugs=[ingestion.topic_slug],
        top_k=2, report_depth="standard",
    )
    old = repo.jobs.claim_resource_job("report", report.id, lease_seconds=-1)
    assert repo.jobs.claim_resource_job("report", report.id) is None
    repo.reports.reset_report_for_retry(report.id)
    with pytest.raises(StaleReportExecution):
        repo.reports.finalize_report_execution(
            report.id, old.id, expected_attempt=old.attempts,
            expected_owner=old.lease_owner, status="failed", error="stale",
        )
    assert worker.run_once()
    assert repo.reports.get_report(report.id).status == "completed"
    assert repo.jobs.get_job(old.id).crash_recoveries == 0


def test_concurrent_reclaim_counts_each_expired_execution_once(tmp_path):
    repo = KnowledgeRepository(str(tmp_path / "concurrent.db"), max_crash_recoveries=2)
    job = repo.jobs.enqueue_job(kind="ingestion", resource_id="concurrent")
    repo.jobs.claim_resource_job("ingestion", job.resource_id, lease_seconds=-1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(
            lambda owner: repo.jobs.claim_resource_job(
                "ingestion", job.resource_id, owner_id=owner
            ),
            ["first", "second"],
        ))
    assert sum(item is not None for item in claimed) == 1
    saved = repo.jobs.get_job(job.id)
    assert saved.crash_recoveries == 1 and saved.attempts == 2


def test_orchestration_quarantine_can_be_manually_retried(tmp_path):
    repo = KnowledgeRepository(str(tmp_path / "command.db"), max_crash_recoveries=1)
    commands = ResearchCommandRepository(repo)
    command, _ = commands.create_or_get(
        idempotency_key="poison-command",
        normalized_request={"mode": "quick_report", "instruction": "研究问题"},
    )
    old = repo.jobs.claim_job(lease_seconds=-1)
    assert repo.jobs.claim_job() is None
    assert commands.get(command.id).status == "failed"
    assert commands.get(command.id).error == CRASH_RECOVERY_ERROR_MESSAGE
    assert commands.retry_orchestration(command.id).status == "accepted"
    current = repo.jobs.claim_job()
    assert current.id == old.id and current.attempts == 2
    assert current.crash_recoveries == 0


@pytest.mark.parametrize("outcome", ["complete_job", "fail_job"])
def test_normal_terminal_writes_do_not_consume_crash_budget(tmp_path, outcome):
    repo = KnowledgeRepository(str(tmp_path / "normal.db"), max_crash_recoveries=1)
    job = repo.jobs.enqueue_job(kind="ingestion", resource_id="normal")
    claimed = repo.jobs.claim_job()
    args = (job.id, "ordinary exception") if outcome == "fail_job" else (job.id,)
    assert getattr(repo.jobs, outcome)(
        *args, expected_attempt=claimed.attempts, expected_owner=claimed.lease_owner
    )
    repo.recover_running_work()
    assert repo.jobs.claim_job() is None
    saved = repo.jobs.get_job(job.id)
    assert saved.status == ("completed" if outcome == "complete_job" else "failed")
    assert saved.crash_recoveries == 0


@pytest.mark.parametrize("path", ["worker", "resource", "startup"])
def test_agent_run_quarantine_is_audited_and_manual_resume_executes(tmp_path, path):
    repo, service, _, worker, project, task, _ = _runtime_stack(tmp_path)
    repo.jobs.max_crash_recoveries = 2
    run = service.create_run(project.id, task.id, _foundation_request())

    def claim():
        if path == "worker":
            return repo.jobs.claim_job(lease_seconds=-1, owner_id="same-worker")
        if path == "startup":
            repo.recover_running_work()
        return repo.jobs.claim_resource_job(
            "agent_run", run.id, lease_seconds=-1, owner_id="same-worker"
        )

    old = claim()
    with service.execution_scope(ExecutionFence.from_job(old)):
        service.mark_node(run.id, status="preparing", node_name="load_context", input_summary={})
    current = claim()
    assert current.attempts == 2 and current.crash_recoveries == 1
    with service.execution_scope(ExecutionFence.from_job(old)):
        with pytest.raises(AgentRunConflictError, match="no longer owned"):
            service.mark_node(run.id, status="running", node_name="stale", input_summary={})
    assert claim() is None
    failed = service.get_run(run.id)
    assert failed.status == "failed"
    assert failed.error_code == CRASH_RECOVERY_ERROR_CODE
    assert failed.error_message == CRASH_RECOVERY_ERROR_MESSAGE
    events = service.repository.list_events(run.id)
    assert events[-1].event_type == "run_failed"
    assert events[-1].error["code"] == CRASH_RECOVERY_ERROR_CODE
    assert claim() is None
    assert service.repository.list_events(run.id) == events
    assert service.resume_run(run.id).status == "queued"
    assert repo.jobs.get_job(old.id).crash_recoveries == 0
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert repo.jobs.get_job(old.id).attempts == 3
    with service.execution_scope(ExecutionFence.from_job(current)):
        with pytest.raises(AgentRunConflictError, match="no longer owned"):
            service.mark_node(run.id, status="running", node_name="stale", input_summary={})


def test_recovery_preserves_completed_business_records(tmp_path):
    repo, service, _, worker, project, task, _ = _runtime_stack(tmp_path)
    run = service.create_run(project.id, task.id, _foundation_request())
    assert worker.run_once()
    before = service.get_run(run.id)
    repo.jobs.enqueue_job(kind="agent_run", resource_id=run.id, force_requeue=True)
    repo.jobs.max_crash_recoveries = 1
    repo.jobs.claim_resource_job("agent_run", run.id, lease_seconds=-1)
    assert repo.jobs.claim_resource_job("agent_run", run.id) is None
    assert service.get_run(run.id) == before
    saved = repo.jobs.get_resource_job("agent_run", run.id)
    assert saved.status == "completed" and saved.crash_recoveries == 0


def test_agent_failure_before_queue_acknowledgement_preserves_normal_error(tmp_path):
    repo, service, _, _, project, task, _ = _runtime_stack(tmp_path)
    run = service.create_run(project.id, task.id, _foundation_request())
    old = repo.jobs.claim_resource_job("agent_run", run.id, lease_seconds=-1)
    with service.execution_scope(ExecutionFence.from_job(old)):
        failed = service.fail_run(run.id, error_code="normal_failure", error_message="正常异常")
    repo.recover_running_work()
    assert service.get_run(run.id) == failed
    saved = repo.jobs.get_job(old.id)
    assert saved.status == "failed" and saved.crash_recoveries == 0
    assert saved.last_error == "正常异常"


def test_migration_preserves_old_jobs_and_is_repeatable(tmp_path, monkeypatch):
    path = str(tmp_path / "v21.db")
    original = KnowledgeRepository._apply_structural_migration
    with monkeypatch.context() as patch:
        patch.setattr(
            KnowledgeRepository, "_apply_structural_migration",
            lambda self, version: original(self, version) if version < 22 else None,
        )
        KnowledgeRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO knowledge_jobs
            (id, kind, resource_id, status, payload_json, attempts, created_at, updated_at)
            VALUES ('old', 'ingestion', 'source', 'running', '{}', 100, 't', 't')"""
        )
    for _ in range(2):
        repo = KnowledgeRepository(path)
        assert repo.schema_version() == 22
        job = repo.jobs.get_job("old")
        assert job.attempts == 100 and job.crash_recoveries == 0
        assert job.status == "running"
    claimed = repo.jobs.claim_job()
    assert claimed.id == "old" and claimed.attempts == 101
    assert claimed.crash_recoveries == 1
