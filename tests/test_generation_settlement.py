"""Late model responses may settle accounting, but cannot publish business work."""

import json

import pytest

from app.agent.errors import AgentRunConflictError, AgentRunTerminalError
from app.agent.models import AgentRunCreateRequest
from app.execution import ExecutionFence
from app.knowledge.repository import KnowledgeRepository
from app.runtime.job_repository import CRASH_RECOVERY_ERROR_CODE
from tests.test_a08_production import report
from tests.test_research_workflow import _analysis, _plan, _research_stack


def _attempt(repo, run_id, slot=0):
    with repo.database.connect() as db:
        return dict(db.execute(
            "SELECT * FROM research_generation_attempts WHERE run_id=? AND slot=?", (run_id, slot)
        ).fetchone())


def _business_state(repo):
    with repo.database.connect() as db:
        return {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in (
                "agent_runs", "agent_run_events", "agent_run_outputs", "agent_tool_calls",
                "artifacts", "memory_proposals", "knowledge_jobs",
            )
        }


@pytest.mark.parametrize("workflow", ["research_v2", "research_v9", "research_v11"])
@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_cancel_during_model_call_settles_without_business_writes(
    tmp_path, monkeypatch, workflow, repair, failed,
):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [] if workflow == "research_v9" else [_analysis(), _plan()]
    )
    if repair:
        llm.responses.append("invalid json")
    llm.responses.append(TimeoutError() if failed else json.dumps(report(bundle)))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow=workflow, token_budget=32000,
    ))
    original_invoke = llm.invoke
    cancel_at = (0 if workflow == "research_v9" else 2) + int(repair)
    captured = {}

    def invoke(*args, **kwargs):
        if len(llm.calls) == cancel_at:
            captured["fence"] = service._current_execution.get()
            service.cancel_run(run.id)
            captured["business"] = _business_state(repo)
        return original_invoke(*args, **kwargs)

    original_finish = service.finish_generation_attempt

    def finish(*args, **kwargs):
        captured["settlement"] = (args, kwargs)
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(llm, "invoke", invoke)
    monkeypatch.setattr(service, "finish_generation_attempt", finish)
    assert worker.run_once()
    assert service.get_run(run.id).status == "cancelled"
    assert _business_state(repo) == captured["business"]
    assert len(llm.calls) == cancel_at + 1
    row = _attempt(repo, run.id, int(repair))
    assert row["status"] == ("failed" if failed else "returned")
    assert row["audit_only"] == 1
    assert row["error_type"] == ("TimeoutError" if failed else None)
    assert row["response"] == (None if failed else json.dumps(report(bundle)))
    assert json.loads(row["usage_json"]) == llm.usage
    totals, _ = service.repository.trace_totals(run.id)
    assert totals == {
        "input_tokens": len(llm.calls) * 11,
        "output_tokens": len(llm.calls) * 7,
        "total_tokens": len(llm.calls) * 18,
    }
    with pytest.raises(KeyError):
        service.repository.get_output(run.id)
    assert captured["business"]["artifacts"] == []
    assert captured["business"]["memory_proposals"] == []
    # A retried delivery cannot replace the first response or inflate usage.
    args, kwargs = captured["settlement"]
    with service.execution_scope(captured["fence"]):
        assert original_finish(*args, **{
            **kwargs, "response": "different retry", "usage": {"input_tokens": 999},
        }) is False
        with pytest.raises(AgentRunConflictError):
            original_finish(*args, **{**kwargs, "digest": "different prompt"})
        with pytest.raises(AgentRunConflictError):
            original_finish(run.id, 1 - int(repair), **kwargs)
        with pytest.raises(AgentRunConflictError):
            service.mark_node(run.id, status="running", node_name="late", input_summary={})
        with pytest.raises(AgentRunConflictError):
            service.record_node_completed(run.id, node_name="late", output_summary={})
    assert _attempt(repo, run.id, int(repair)) == row
    assert service.repository.trace_totals(run.id)[0] == totals
    assert _business_state(repo) == captured["business"]


def _issued(tmp_path, *, fenced=True, owner="old"):
    repo, service, _, project, task, _, _, _ = _research_stack(tmp_path, [])
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v11", token_budget=32000,
    ))
    job = repo.jobs.claim_resource_job("agent_run", run.id, lease_seconds=-1, owner_id=owner)
    fence = ExecutionFence.from_job(job) if fenced else None
    with service.execution_scope(fence):
        service.begin_generation_attempt(run.id, 0, "prompt")
    return repo, service, run, fence


def _finish(service, run_id, **kwargs):
    return service.finish_generation_attempt(
        run_id, 0, digest="prompt", response="response", error_type=None,
        usage=kwargs.pop("usage", {"input_tokens": 10, "output_tokens": 2}),
        prompt_tokens=10, **kwargs,
    )


@pytest.mark.parametrize("owner", ["new", "old"])
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("fenced", [False, True])
def test_reclaimed_attempt_rejects_old_result_even_for_same_prompt_and_owner(
    tmp_path, owner, cancelled, fenced,
):
    repo, service, run, old = _issued(tmp_path, fenced=fenced)
    current_job = repo.jobs.claim_resource_job("agent_run", run.id, owner_id=owner)
    current = ExecutionFence.from_job(current_job)
    with service.execution_scope(current):
        # Recovery deliberately does not reissue an ambiguous pending call.
        assert not service.begin_generation_attempt(run.id, 0, "prompt")["created"]
        with pytest.raises(AgentRunConflictError):
            _finish(service, run.id)
    if cancelled:
        service.cancel_run(run.id)
    row = _attempt(repo, run.id)
    business = _business_state(repo)
    with service.execution_scope(old), pytest.raises(AgentRunConflictError):
        _finish(service, run.id)
    assert _attempt(repo, run.id) == row
    assert row["status"] == "pending"
    assert _business_state(repo) == business
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == 0


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("known_usage", [False, True])
@pytest.mark.parametrize("fenced", [False, True])
def test_revocation_or_unfenced_cancellation_only_updates_audit(
    tmp_path, cancelled, known_usage, fenced,
):
    repo, service, run, fence = _issued(tmp_path, fenced=fenced)
    if cancelled:
        service.cancel_run(run.id)
    else:
        repo.jobs.complete_resource_job("agent_run", run.id)
    business = _business_state(repo)
    with service.execution_scope(fence):
        usage = {"input_tokens": 10} if known_usage else None
        assert _finish(service, run.id, usage=usage) is False
        assert _finish(service, run.id) is False
    row = _attempt(repo, run.id)
    assert row["status"] == "returned" and row["audit_only"] == 1
    assert json.loads(row["usage_json"]) == ({"input_tokens": 10} if known_usage else None)
    assert _business_state(repo) == business
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == (10 if known_usage else 0)


def test_v22_migration_preserves_ledger_and_does_not_guess_legacy_ownership(tmp_path):
    repo, service, run, fence = _issued(tmp_path)
    with repo.database.connect() as db:
        db.execute("ALTER TABLE research_generation_attempts DROP COLUMN execution_identity_json")
        db.execute("ALTER TABLE research_generation_attempts DROP COLUMN audit_only")
        db.execute("DELETE FROM schema_migrations WHERE version=23")
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 22
        crash_recoveries = repo.jobs.get_resource_job("agent_run", run.id).crash_recoveries
        before = dict(db.execute("SELECT * FROM research_generation_attempts").fetchone())
    for _ in range(2):
        assert KnowledgeRepository(repo.path).schema_version() == 23
    row = _attempt(repo, run.id)
    assert all(row[key] == value for key, value in before.items())
    assert row["execution_identity_json"] is None
    assert repo.jobs.get_resource_job("agent_run", run.id).crash_recoveries == crash_recoveries
    service.cancel_run(run.id)
    with service.execution_scope(fence), pytest.raises(AgentRunConflictError):
        _finish(service, run.id)
    assert _attempt(repo, run.id) == row


def test_other_terminal_statuses_do_not_allow_pending_settlement(tmp_path):
    repo, service, run, fence = _issued(tmp_path)
    with repo.database.connect() as db:
        db.execute("UPDATE agent_runs SET status='needs_review' WHERE id=?", (run.id,))
    with service.execution_scope(fence), pytest.raises(AgentRunTerminalError):
        _finish(service, run.id)
    assert _attempt(repo, run.id)["status"] == "pending"


def test_cancel_before_queue_revocation_still_settles_over_budget_call(tmp_path):
    repo, service, run, fence = _issued(tmp_path)
    with repo.database.connect() as db:
        db.execute("UPDATE agent_runs SET token_budget=1 WHERE id=?", (run.id,))
    # Exercise the window between the business cancellation and queue revocation.
    service.repository.cancel_run(run.id)
    business = _business_state(repo)
    with service.execution_scope(fence):
        assert _finish(service, run.id) is False
    assert _attempt(repo, run.id)["status"] == "returned"
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == 12
    assert _business_state(repo) == business


def test_normal_settlement_remains_idempotent_after_completion(tmp_path):
    repo, service, run, fence = _issued(tmp_path)
    with service.execution_scope(fence):
        assert _finish(service, run.id)
    with repo.database.connect() as db:
        db.execute("UPDATE agent_runs SET status='completed' WHERE id=?", (run.id,))
    repo.jobs.complete_resource_job("agent_run", run.id)
    row, business = _attempt(repo, run.id), _business_state(repo)
    with service.execution_scope(fence):
        assert _finish(service, run.id) is False
    assert _attempt(repo, run.id) == row
    assert _business_state(repo) == business
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == 12


def test_budget_checks_include_revoked_usage_after_recovery(tmp_path):
    repo, service, run, fence = _issued(tmp_path)
    repo.jobs.complete_resource_job("agent_run", run.id)
    with service.execution_scope(fence):
        assert _finish(service, run.id) is False
    service.fail_run(run.id, error_code="interrupted", error_message="interrupted")
    service.resume_run(run.id)
    job = repo.jobs.claim_resource_job("agent_run", run.id)
    with repo.database.connect() as db:
        db.execute("UPDATE agent_runs SET token_budget=12 WHERE id=?", (run.id,))
    business = _business_state(repo)
    with service.execution_scope(ExecutionFence.from_job(job)):
        with pytest.raises(AgentRunConflictError, match="token budget"):
            service.record_node_completed(
                run.id, node_name="task_analysis", output_summary={},
                token_usage={"input_tokens": 1},
            )
    assert _business_state(repo) == business


@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("reclaimed", [False, True])
def test_runtime_stops_after_revocation_without_publishing_failure(
    tmp_path, monkeypatch, fenced, reclaimed,
):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    llm.responses.append(json.dumps(report(bundle)))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v11", token_budget=32000,
    ))
    original = llm.invoke
    captured = {}

    def invoke(*args, **kwargs):
        if len(llm.calls) == 2:
            repo.jobs.complete_resource_job("agent_run", run.id)
            if reclaimed:
                repo.jobs.enqueue_job(kind="agent_run", resource_id=run.id, force_requeue=True)
                repo.jobs.claim_resource_job("agent_run", run.id, owner_id="replacement")
            captured["business"] = _business_state(repo)
        return original(*args, **kwargs)

    monkeypatch.setattr(llm, "invoke", invoke)
    if fenced:
        assert worker.run_once()
    else:
        worker.agent_runtime.execute_queued(run.id)
    assert _business_state(repo) == captured["business"]
    assert len(llm.calls) == 3
    row = _attempt(repo, run.id)
    assert row["status"] == ("pending" if reclaimed else "returned")
    assert row["audit_only"] == (0 if reclaimed else 1)
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == (36 if reclaimed else 54)


@pytest.mark.parametrize("fenced", [False, True])
def test_quarantined_attempt_only_audits_late_result_and_rejects_after_resume(tmp_path, fenced):
    repo, service, run, fence = _issued(tmp_path, fenced=fenced)
    repo.jobs.max_crash_recoveries = 1
    assert repo.jobs.claim_resource_job("agent_run", run.id) is None
    assert service.get_run(run.id).error_code == CRASH_RECOVERY_ERROR_CODE
    business = _business_state(repo)
    with service.execution_scope(fence):
        assert _finish(service, run.id) is False
        assert _finish(service, run.id, usage={"input_tokens": 999}) is False
    row = _attempt(repo, run.id)
    assert row["status"] == "returned" and row["audit_only"] == 1
    assert json.loads(row["usage_json"]) == {"input_tokens": 10, "output_tokens": 2}
    assert _business_state(repo) == business

    service.resume_run(run.id)
    repo.jobs.claim_resource_job("agent_run", run.id, owner_id="old")
    business = _business_state(repo)
    with service.execution_scope(fence), pytest.raises(AgentRunConflictError):
        _finish(service, run.id)
    assert _attempt(repo, run.id) == row
    assert _business_state(repo) == business
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == 12


@pytest.mark.parametrize("failed", [False, True])
def test_worker_does_not_publish_after_quarantine_during_model_call(tmp_path, monkeypatch, failed):
    repo, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    llm.responses.append(TimeoutError() if failed else json.dumps(report(bundle)))
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v11", token_budget=32000,
    ))
    repo.jobs.max_crash_recoveries = 1
    original = llm.invoke
    captured = {}

    def invoke(*args, **kwargs):
        if len(llm.calls) == 2:
            # Expire the live worker while its model request is in flight.
            with repo.database.connect() as db:
                db.execute(
                    "UPDATE knowledge_jobs SET lease_until=NULL WHERE kind='agent_run' "
                    "AND resource_id=?", (run.id,),
                )
            assert repo.jobs.claim_resource_job("agent_run", run.id) is None
            captured["business"] = _business_state(repo)
        return original(*args, **kwargs)

    monkeypatch.setattr(llm, "invoke", invoke)
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    assert service.get_run(run.id).error_code == CRASH_RECOVERY_ERROR_CODE
    assert _business_state(repo) == captured["business"]
    row = _attempt(repo, run.id)
    assert row["status"] == ("failed" if failed else "returned")
    assert row["audit_only"] == 1
    assert row["error_type"] == ("TimeoutError" if failed else None)
    assert service.repository.trace_totals(run.id)[0]["total_tokens"] == 54
    assert len(llm.calls) == 3
