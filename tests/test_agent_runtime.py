from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.errors import AgentRunConflictError, AgentRunTerminalError, AgentToolPermissionError
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.agent.tools import build_foundation_tool_registry
from app.agent.workflow import DeterministicFoundationWorkflow
from app.api.main import create_app
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.execution import ExecutionFence
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeIngestionService
from app.worker import KnowledgeWorker


def _foundation_request(**values) -> AgentRunCreateRequest:
    return AgentRunCreateRequest(workflow="foundation", **values)


def _runtime_stack(tmp_path, *, failure_injector=None):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        agent_checkpoint_path=str(tmp_path / "agent_checkpoints.db"),
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    service = AgentRunService(repository, settings=settings)
    workflow = DeterministicFoundationWorkflow(
        service, build_foundation_tool_registry(), failure_injector=failure_injector
    )
    runtime = AgentRuntime(
        service,
        checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
        workflow=workflow,
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(repository, settings=settings),
        report_service=KnowledgeReportService(
            repository, settings=settings, require_live_llm=False
        ),
        agent_runtime=runtime,
        lease_seconds=30,
    )
    memory = repository.memory_repository
    project = memory.create_project(name="Agent Project", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Foundation task", goal="", priority="normal", metadata={}
    )
    return repository, service, runtime, worker, project, task, Path(settings.agent_checkpoint_path)


def test_agent_run_queue_executes_deterministic_workflow_with_isolated_checkpoint(tmp_path) -> None:
    repository, service, _, worker, project, task, checkpoint_path = _runtime_stack(tmp_path)

    assert not checkpoint_path.exists()
    run = service.create_run(project.id, task.id, _foundation_request())
    assert run.status == "queued"
    with repository._connect() as connection:
        job = connection.execute(
            "SELECT kind, status FROM knowledge_jobs WHERE resource_id = ?", (run.id,)
        ).fetchone()
    assert dict(job) == {"kind": "agent_run", "status": "queued"}

    assert worker.run_once()
    completed = service.get_run(run.id)
    events = service.repository.list_events(run.id)
    tool_calls = service.repository.list_tool_calls(run.id)
    output = service.repository.get_output(run.id)

    assert completed.status == "completed"
    assert checkpoint_path.exists()
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert tool_calls[0].tool_name == "context.snapshot_metadata"
    assert tool_calls[0].result_summary["context_snapshot_id"] == run.context_snapshot_id
    assert output.output_type == "agent_runtime_foundation"
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0


def test_agent_run_recovers_after_failure_from_the_same_checkpoint_thread(tmp_path) -> None:
    failed_once: set[str] = set()

    def fail_once(node_name: str, run_id: str) -> None:
        if node_name == "inspect_context" and run_id not in failed_once:
            failed_once.add(run_id)
            raise RuntimeError("simulated worker interruption")

    repository, service, _, worker, project, task, checkpoint_path = _runtime_stack(
        tmp_path, failure_injector=fail_once
    )
    run = service.create_run(project.id, task.id, _foundation_request())

    assert worker.run_once()
    failed = service.get_run(run.id)
    assert failed.status == "failed"
    assert checkpoint_path.exists()
    assert service.resume_run(run.id).status == "queued"

    safe_workflow = DeterministicFoundationWorkflow(service, build_foundation_tool_registry())
    safe_runtime = AgentRuntime(
        service,
        checkpoint_factory=AgentCheckpointFactory(str(checkpoint_path)),
        workflow=safe_workflow,
    )
    safe_worker = KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(repository),
        report_service=KnowledgeReportService(repository, require_live_llm=False),
        agent_runtime=safe_runtime,
        lease_seconds=30,
    )
    assert safe_worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert len(service.repository.list_tool_calls(run.id)) == 1

    # A reclaimed queue record for a completed run is acknowledged without a
    # second graph execution.  AgentRun remains the business authority.
    assert safe_runtime.execute_queued(run.id).status == "completed"
    assert len(service.repository.list_tool_calls(run.id)) == 1

    repository.enqueue_job(
        kind="agent_run", resource_id=run.id, payload={}, force_requeue=True
    )
    assert safe_worker.run_once()
    with repository._connect() as connection:
        status = connection.execute(
            "SELECT status FROM knowledge_jobs WHERE kind = 'agent_run' AND resource_id = ?",
            (run.id,),
        ).fetchone()["status"]
    assert status == "completed"
    assert len(service.repository.list_tool_calls(run.id)) == 1


def test_agent_run_reclaims_an_interrupted_active_business_state(tmp_path) -> None:
    _, service, runtime, _, project, task, _ = _runtime_stack(tmp_path)
    run = service.create_run(project.id, task.id, _foundation_request())

    with pytest.raises(AgentRunTerminalError, match="expected one of: running"):
        service.mark_node(
            run.id,
            status="validating",
            node_name="complete",
            input_summary={},
        )
    service.mark_node(
        run.id,
        status="preparing",
        node_name="load_context",
        input_summary={"simulated": "worker restart"},
    )
    completed = runtime.execute_queued(run.id)

    assert completed.status == "completed"
    assert any(
        event.event_type == "run_recovered" for event in service.repository.list_events(run.id)
    )


def test_stale_agent_worker_cannot_write_after_job_is_reclaimed(tmp_path) -> None:
    repository, service, runtime, _, project, task, _ = _runtime_stack(tmp_path)
    run = service.create_run(project.id, task.id, _foundation_request())
    stale = repository.claim_resource_job(
        "agent_run", run.id, lease_seconds=-1, owner_id="old-agent-worker"
    )
    current = repository.claim_resource_job(
        "agent_run", run.id, lease_seconds=30, owner_id="new-agent-worker"
    )
    assert stale is not None and current is not None

    with service.execution_scope(ExecutionFence.from_job(stale)):
        with pytest.raises(AgentRunConflictError, match="no longer owned"):
            service.mark_node(
                run.id,
                status="preparing",
                node_name="load_context",
                input_summary={"executor": "stale"},
            )

    completed = runtime.execute_queued(
        run.id, execution_fence=ExecutionFence.from_job(current)
    )
    assert completed.status == "completed"
    assert all(
        event.input_summary.get("executor") != "stale"
        for event in service.repository.list_events(run.id)
    )


def test_agent_run_cancel_prevents_queue_execution_and_checkpoint_creation(tmp_path) -> None:
    _, service, runtime, worker, project, task, checkpoint_path = _runtime_stack(tmp_path)
    run = service.create_run(project.id, task.id, _foundation_request())

    cancelled = service.cancel_run(run.id)
    assert cancelled.status == "cancelled"
    assert runtime.execute_queued(run.id).status == "cancelled"
    assert not worker.run_once()
    assert service.get_run(run.id).status == "cancelled"
    assert not checkpoint_path.exists()


def test_agent_run_honours_a_zero_tool_call_budget(tmp_path) -> None:
    _, service, _, worker, project, task, _ = _runtime_stack(tmp_path)
    run = service.create_run(
        project.id, task.id, _foundation_request(max_tool_calls=0)
    )

    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    assert service.repository.list_tool_calls(run.id) == []
    assert service.repository.get_output(run.id).structured["tool_result"]["skipped"]


def test_agent_run_api_exposes_lifecycle_and_keeps_checkpoint_lazy(tmp_path) -> None:
    repository, service, _, _, project, task, checkpoint_path = _runtime_stack(tmp_path)
    app = create_app(knowledge_repository=repository, agent_run_service=service)

    with TestClient(app) as client:
        assert not checkpoint_path.exists()
        created = client.post(
            f"/api/projects/{project.id}/workspace-tasks/{task.id}/agent-runs",
            json={"workflow": "foundation"},
        )
        run_id = created.json()["id"]
        duplicate = client.post(
            f"/api/projects/{project.id}/workspace-tasks/{task.id}/agent-runs",
            json={"workflow": "foundation"},
        )
        events = client.get(f"/api/agent-runs/{run_id}/events")
        no_output = client.get(f"/api/agent-runs/{run_id}/output")
        cancelled = client.post(f"/api/agent-runs/{run_id}/cancel")
        not_recoverable = client.post(f"/api/agent-runs/{run_id}/resume")
        missing = client.get("/api/agent-runs/not-found")
        invalid_steps = client.post(
            f"/api/projects/{project.id}/workspace-tasks/{task.id}/agent-runs",
            json={"workflow": "foundation", "max_steps": 2},
        )

    assert created.status_code == 202
    assert duplicate.status_code == 409
    assert events.status_code == 200
    assert no_output.status_code == 404
    assert cancelled.json()["status"] == "cancelled"
    assert not_recoverable.status_code == 409
    assert missing.status_code == 404
    assert invalid_steps.status_code == 422
    assert not checkpoint_path.exists()


def test_agent_run_allows_only_one_active_run_per_workspace_task(tmp_path) -> None:
    _, service, _, _, project, task, _ = _runtime_stack(tmp_path)

    def create():
        try:
            return service.create_run(project.id, task.id, _foundation_request())
        except AgentRunConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))

    runs = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, AgentRunConflictError)]
    assert len(runs) == 1
    assert len(conflicts) == 1


def test_tool_registry_enforces_registered_permissions() -> None:
    registry = build_foundation_tool_registry()
    with pytest.raises(AgentToolPermissionError, match="does not permit"):
        registry.execute(
            name="context.snapshot_metadata",
            permission="runtime_read",
            arguments={"context_snapshot_id": "snapshot", "context_sha256": "hash"},
        )
    with pytest.raises(AgentToolPermissionError, match="not registered"):
        registry.execute(name="unknown", permission="context_read", arguments={})


def test_v12_to_v16_agent_runtime_migrations_are_additive_and_idempotent(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    project = memory.create_project(name="Migration", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Task", goal="", priority="normal", metadata={}
    )
    snapshot = ContextBuilderService(repository).build_context(
        ContextBuildRequest(task_id=task.id, project_id=project.id)
    )
    with repository._connect() as connection:
        context_before = dict(
            connection.execute(
                "SELECT * FROM context_snapshots WHERE id = ?", (snapshot.snapshot_id,)
            ).fetchone()
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE agent_run_outputs")
        connection.execute("DROP TABLE agent_tool_calls")
        connection.execute("DROP TABLE agent_run_events")
        connection.execute("DROP TABLE agent_runs")
        connection.execute("DROP TABLE project_domain_plugins")
        connection.execute("DROP INDEX workspace_tasks_plugin_idx")
        connection.execute("ALTER TABLE workspace_tasks DROP COLUMN domain_plugin_key")
        connection.execute("DROP TABLE executor_heartbeats")
        connection.execute("DROP INDEX knowledge_jobs_claim_idx")
        connection.execute("ALTER TABLE knowledge_jobs DROP COLUMN priority")
        connection.execute("ALTER TABLE knowledge_jobs DROP COLUMN lease_owner")
        connection.execute("ALTER TABLE projection_outbox DROP COLUMN lease_owner")
        connection.execute("DELETE FROM schema_migrations WHERE version = 16")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("DELETE FROM schema_migrations WHERE version = 14")
        connection.execute("DELETE FROM schema_migrations WHERE version = 13")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 12

    upgraded = KnowledgeRepository(repository.path)
    assert upgraded.schema_version() == 16
    with upgraded._connect() as connection:
        assert dict(
            connection.execute(
                "SELECT * FROM context_snapshots WHERE id = ?", (snapshot.snapshot_id,)
            ).fetchone()
        ) == context_before
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
        assert {
            row["name"] for row in connection.execute("PRAGMA table_info(agent_runs)")
        } >= {
            "options_json",
            "plugin_key",
            "plugin_version",
            "plugin_contract_version",
            "plugin_workflow_key",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert KnowledgeRepository(repository.path).schema_version() == 16
