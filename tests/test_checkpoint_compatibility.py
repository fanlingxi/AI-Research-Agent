from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.agent.errors import (
    AgentRunConflictError,
    AgentRunNotRecoverableError,
    WorkflowVersionMismatchError,
)
from app.api.main import create_app
from app.execution import ExecutionFence
from tests.test_agent_runtime import _foundation_request, _runtime_stack
from tests.test_research_workflow import (
    _analysis,
    _draft,
    _plan,
    _research_request,
    _research_stack,
)


def _change_contract(service, run, change):
    plugin = service.plugin_registry.get(run.plugin.key)
    specification = plugin.workflow_spec(run.plugin.workflow_key)
    if change in {"version", "checkpoint_namespace", "name", "key"}:
        changed = specification.model_copy(update={change: f"{getattr(specification, change)}_new"})
        plugin.manifest = plugin.manifest.model_copy(update={
            "workflows": [changed if item.key == specification.key else item
                          for item in plugin.manifest.workflows],
        })
    else:
        with service.repository._connect() as connection:
            if change == "missing_version":
                # Version has always been NOT NULL; an empty value is the
                # only representable missing version in a supported database.
                connection.execute("UPDATE agent_runs SET workflow_version = '' WHERE id = ?",
                                   (run.id,))
            else:
                options = run.options.model_dump()
                if change == "missing_namespace":
                    options.pop("checkpoint_namespace")
                elif change == "options_key":
                    options["workflow"] = "foundation"
                connection.execute("UPDATE agent_runs SET options_json = ? WHERE id = ?",
                                   (json.dumps(options), run.id))


@pytest.mark.parametrize("entry", ["queued", "reclaimed", "manual", "api"])
@pytest.mark.parametrize("change", [
    "version", "checkpoint_namespace", "name", "key", "missing_version", "options_key",
])
def test_incompatible_checkpoint_stops_before_any_graph_or_model_work(tmp_path, entry, change):
    repository, service, worker, project, task, package, _, llm = _research_stack(tmp_path, [])
    llm.responses = [json.dumps(_analysis()), json.dumps(_plan()), RuntimeError("interrupted")]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    assert len(llm.calls) == 3
    checkpoint_path = Path(worker.agent_runtime.checkpoint_factory.path)
    checkpoint_before = checkpoint_path.read_bytes()
    snapshot_before = service.load_context_snapshot(run.id).model_dump_json()
    events_before = len(service.repository.list_events(run.id))
    if entry in {"queued", "reclaimed"}:
        assert service.resume_run(run.id).status == "queued"
        if entry == "reclaimed":
            # The graph had started when the old worker died. The next claim
            # must not recover it into the new graph implementation.
            with repository._connect() as connection:
                connection.execute("UPDATE agent_runs SET status = 'running' WHERE id = ?",
                                   (run.id,))
    _change_contract(service, run, change)
    plugin = service.plugin_registry.get(run.plugin.key)
    plugin.build_workflow = Mock(side_effect=AssertionError("Must not build the graph"))
    worker.agent_runtime.checkpoint_factory.open = Mock(
        side_effect=AssertionError("Must not open a checkpoint"),
    )
    if entry in {"queued", "reclaimed"}:
        assert worker.run_once()
        stopped = service.get_run(run.id)
    elif entry == "manual":
        stopped = service.resume_run(run.id)
    else:
        app = create_app(knowledge_repository=repository, agent_run_service=service)
        with TestClient(app) as client:
            response = client.post(f"/api/agent-runs/{run.id}/resume")
        assert response.status_code == 202
        assert response.json()["status"] == "needs_review"
        assert response.json()["error_code"] == WorkflowVersionMismatchError.code
        stopped = service.get_run(run.id)
    assert stopped.status == "needs_review"
    assert stopped.error_code == WorkflowVersionMismatchError.code
    assert stopped.error_message == WorkflowVersionMismatchError.message
    assert stopped.current_node == "workflow_compatibility_check"
    assert stopped.completed_at is not None
    assert len(llm.calls) == 3
    plugin.build_workflow.assert_not_called()
    worker.agent_runtime.checkpoint_factory.open.assert_not_called()
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert service.load_context_snapshot(run.id).model_dump_json() == snapshot_before
    with pytest.raises(KeyError, match="has no output"):
        service.repository.get_output(run.id)
    new_events = service.repository.list_events(run.id)[events_before:]
    assert not any(event.event_type == "node_started" for event in new_events)
    assert new_events[-1].error["code"] == WorkflowVersionMismatchError.code
    with repository._connect() as connection:
        job = connection.execute(
            "SELECT status, lease_owner, lease_until FROM knowledge_jobs WHERE resource_id = ?",
            (run.id,),
        ).fetchone()
    assert dict(job) == {"status": "completed", "lease_owner": None, "lease_until": None}
    assert not worker.run_once()
    assert worker.agent_runtime.execute_queued(run.id) == stopped
    with pytest.raises(AgentRunNotRecoverableError):
        service.resume_run(run.id)
    # Even an explicitly redelivered queue record is acknowledged once.
    repository.jobs.enqueue_job(kind="agent_run", resource_id=run.id, payload={},
                                force_requeue=True)
    assert worker.run_once()
    assert not worker.run_once()
    assert service.get_run(run.id) == stopped


@pytest.mark.parametrize("change", ["version", "checkpoint_namespace"])
def test_new_run_uses_updated_contract_and_the_same_immutable_snapshot(tmp_path, change):
    _, service, worker, project, task, package, bundle, llm = _research_stack(tmp_path, [])
    old = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    _change_contract(service, old, change)
    assert worker.run_once()
    assert service.get_run(old.id).status == "needs_review"
    assert llm.calls == []
    llm.responses = [json.dumps(_analysis()), json.dumps(_plan()),
                     json.dumps(_draft(bundle.claim.claim_id,
                                       bundle.evidence[0].evidence_id, valid=True))]
    new = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert new.context_snapshot_id == old.context_snapshot_id
    assert new.context_sha256 == old.context_sha256
    assert worker.run_once()
    assert service.get_run(new.id).status == "completed"
    assert len(llm.calls) == 3


def test_completed_report_is_unchanged_when_installed_workflow_changes(tmp_path):
    _, service, worker, project, task, package, bundle, llm = _research_stack(tmp_path, [])
    llm.responses = [json.dumps(_analysis()), json.dumps(_plan()),
                     json.dumps(_draft(bundle.claim.claim_id,
                                       bundle.evidence[0].evidence_id, valid=True))]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert worker.run_once()
    completed = service.get_run(run.id)
    output_before = service.repository.get_output(run.id)
    snapshot_before = service.load_context_snapshot(run.id)
    _change_contract(service, run, "version")
    assert worker.agent_runtime.execute_queued(run.id) == completed
    assert service.repository.get_output(run.id) == output_before
    assert service.load_context_snapshot(run.id) == snapshot_before
    assert len(llm.calls) == 3


def test_matching_contract_resumes_via_api_without_replaying_completed_nodes(tmp_path):
    repository, service, worker, project, task, package, bundle, llm = _research_stack(tmp_path, [])
    llm.responses = [json.dumps(_analysis()), json.dumps(_plan()), RuntimeError("interrupted")]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert run.options.checkpoint_namespace == "agent_runtime_phase3b"
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    app = create_app(knowledge_repository=repository, agent_run_service=service)
    with TestClient(app) as client:
        response = client.post(f"/api/agent-runs/{run.id}/resume")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    llm.responses = [json.dumps(_draft(bundle.claim.claim_id,
                                      bundle.evidence[0].evidence_id, valid=True))]
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert len(llm.calls) == 4
    assert len(service.repository.list_tool_calls(run.id)) == 1


def test_incompatibility_transition_respects_worker_lease_fencing(tmp_path):
    repository, service, runtime, _, project, task, checkpoint_path = _runtime_stack(tmp_path)
    run = service.create_run(project.id, task.id, _foundation_request())
    stale = repository.jobs.claim_resource_job(
        "agent_run", run.id, lease_seconds=-1, owner_id="old-worker",
    )
    current = repository.jobs.claim_resource_job(
        "agent_run", run.id, lease_seconds=30, owner_id="new-worker",
    )
    _change_contract(service, run, "checkpoint_namespace")
    with pytest.raises(AgentRunConflictError, match="no longer owned"):
        runtime.execute_queued(run.id, execution_fence=ExecutionFence.from_job(stale))
    assert service.get_run(run.id).status == "queued"
    stopped = runtime.execute_queued(run.id, execution_fence=ExecutionFence.from_job(current))
    assert stopped.status == "needs_review"
    assert stopped.error_code == WorkflowVersionMismatchError.code
    assert not checkpoint_path.exists()


def test_legacy_run_without_namespace_resumes_when_version_matches(tmp_path):
    repository, service, worker, project, task, package, _, llm = _research_stack(tmp_path, [])
    llm.responses = [json.dumps(_analysis()), json.dumps(_plan()), RuntimeError("interrupted")]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    _change_contract(service, service.get_run(run.id), "missing_namespace")
    assert service.resume_run(run.id).status == "queued"
