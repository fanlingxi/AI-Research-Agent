"""Offline default-policy coverage through real snapshot and run persistence."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.agent.models import AgentRunCreateRequest
from app.api.main import create_app
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.research_commands.models import ResearchCommandSubmission
from app.research_profiles import CLASSIC, STANDARD
from app.workspace.schemas import ContextProjectionRequest
from app.workspace.service import WorkspaceProjectionService
from tests.test_research_commands import _LiveFixtureLLM, _stack
from tests.test_workspace_projection_api import _workspace_stack


def stack(tmp_path):
    repository, agents, project, task, _ = _workspace_stack(tmp_path)
    agents.llm = _LiveFixtureLLM()
    workspace = WorkspaceProjectionService(
        repository, context_builder=agents.context_builder, agent_run_service=agents,
    )
    return repository, agents, workspace, project, task


def test_default_and_environment_rollback(monkeypatch):
    monkeypatch.delenv("RESEARCH_DEFAULT_PROFILE", raising=False)
    assert Settings(_env_file=None).research_default_profile == "standard"
    monkeypatch.setenv("RESEARCH_DEFAULT_PROFILE", "classic")
    assert Settings(_env_file=None).research_default_profile == "classic"


@pytest.mark.parametrize("profile", [STANDARD, CLASSIC])
def test_workspace_preview_freeze_and_run_use_profile(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("RESEARCH_DEFAULT_PROFILE", profile.name)
    repository, agents, workspace, project, task = stack(tmp_path)
    preview = workspace.preview_context(project.id, task.id, ContextProjectionRequest())
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 0
    snapshot = workspace.create_context_snapshot(project.id, task.id, ContextProjectionRequest())
    package = agents.context_builder.snapshot_repository.get(snapshot.id)
    assert package.reading_format == ("inline-v1" if profile == STANDARD else "legacy")
    assert snapshot.token_budget == preview.snapshot.token_budget == (
        16000 if profile == STANDARD else 6000
    )
    assert package.retrieval_audit.parameters["vector_enabled"] == (profile == STANDARD)
    if profile == STANDARD:
        assert package.retrieval_audit.parameters["reranking"]["strategy"] == "coverage-v2"
    else:
        assert "reranking" not in package.retrieval_audit.parameters
    assert f"research_profile:{profile.name}" in snapshot.diagnostics["notices"]
    assert package.retrieval_audit.strategy_id == (
        "project-context-dense-v1" if profile == STANDARD else "project-context-legacy-v1"
    )
    run = agents.create_run(
        project.id, task.id, AgentRunCreateRequest(context_snapshot_id=snapshot.id),
    )
    assert run.options.workflow == profile.workflow
    assert run.options.research_profile == profile.name
    assert (run.token_budget, run.max_steps, run.max_tool_calls) == (
        profile.token_budget, profile.max_steps, profile.max_tool_calls,
    )


@pytest.mark.parametrize("failure", ["no_scope", "vector_failure", "not_configured"])
def test_fallback_rebuilds_before_save_and_records_reason(tmp_path, failure):
    repository, agents, workspace, project, task = stack(tmp_path)
    if failure == "no_scope":
        current = repository.memory_repository.get_project(project.id)
        repository.memory_repository.replace_project_knowledge_scopes(
            project.id, [], expected_project_revision=current.revision,
        )
    retriever = Mock()
    retriever.retrieve.side_effect = RuntimeError("offline vector failure")
    agents.context_builder.vector_retriever = None if failure == "not_configured" else retriever
    preview = workspace.preview_context(project.id, task.id, ContextProjectionRequest())
    snapshot = workspace.create_context_snapshot(project.id, task.id, ContextProjectionRequest())
    package = agents.context_builder.snapshot_repository.get(snapshot.id)
    reason = "no_explicit_knowledge_scope" if failure == "no_scope" else "vector_unavailable"
    assert package.reading_format == "legacy"
    assert snapshot.token_budget == preview.snapshot.token_budget == 6000
    assert f"profile_fallback_reason:{reason}" in package.diagnostics.notices
    assert f"profile_fallback_reason:{reason}" in preview.snapshot.diagnostics["notices"]
    assert package.retrieval_audit.strategy_id == "project-context-legacy-v1"
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 1
    if failure == "no_scope":
        retriever.retrieve.assert_not_called()
    elif failure == "vector_failure":
        assert "vector_unavailable" in package.diagnostics.notices
        assert package.knowledge.claim_bundles  # classic recovers formal evidence
    run = agents.create_run(
        project.id, task.id, AgentRunCreateRequest(context_snapshot_id=snapshot.id),
    )
    assert run.options.workflow == "research"
    assert run.token_budget == 6000
    assert run.options.profile_fallback_reason == reason


def test_explicit_choices_and_old_snapshot_are_preserved(tmp_path):
    _, agents, workspace, project, task = stack(tmp_path)
    builder = agents.context_builder
    explicit = ContextBuildRequest(
        project_id=project.id, task_id=task.id, retrieval_strategy="bm25-v1", max_tokens=7000,
    )
    package = builder.build_research_context(explicit, profile="standard")
    assert package.reading_format == "legacy"
    assert package.retrieval_audit.strategy_id == "project-context-bm25-v1"
    assert package.token_usage.budget == 7000
    assert not any(n.startswith("research_profile:") for n in package.diagnostics.notices)
    original = builder.snapshot_repository.get(package.snapshot_id).model_dump()
    run = agents.create_run(project.id, task.id, AgentRunCreateRequest(
        context_snapshot_id=package.snapshot_id,
    ))
    assert run.options.workflow == "research"
    assert run.token_budget == 6000
    assert builder.snapshot_repository.get(package.snapshot_id).model_dump() == original
    agents.cancel_run(run.id)
    snapshot = workspace.create_context_snapshot(
        project.id, task.id, ContextProjectionRequest(max_tokens=8000),
    )
    assert snapshot.token_budget == 8000
    run = agents.create_run(project.id, task.id, AgentRunCreateRequest(
        context_snapshot_id=snapshot.id, workflow="research", token_budget=9000,
        max_steps=9, max_tool_calls=2,
    ))
    assert (run.options.workflow, run.token_budget, run.max_steps, run.max_tool_calls) == (
        "research", 9000, 9, 2,
    )
    agents.cancel_run(run.id)
    run = agents.create_run(project.id, task.id, AgentRunCreateRequest(
        context_snapshot_id=snapshot.id, token_budget=10000, max_tool_calls=2,
    ))
    assert run.options.workflow == "research_v11"
    assert run.token_budget == 10000 and run.max_tool_calls == 2


def test_http_default_run_without_snapshot_and_explicit_retrieval(tmp_path):
    repository, agents, _, project, task = stack(tmp_path)
    app = create_app(knowledge_repository=repository, agent_run_service=agents)
    url = f"/api/projects/{project.id}/workspace-tasks/{task.id}/agent-runs"
    with TestClient(app) as client:
        response = client.post(url, json={})
        assert response.status_code == 202, response.text
        run = response.json()
        assert run["options"]["workflow"] == "research_v11"
        assert run["token_budget"] == 1048576
        agents.cancel_run(run["id"])
        explicit = client.post(url, json={"reading_format": "legacy", "token_budget": 8000})
        assert explicit.status_code == 202, explicit.text
        assert explicit.json()["options"]["workflow"] == "research"
        assert explicit.json()["token_budget"] == 8000


@pytest.mark.parametrize("profile", [STANDARD, CLASSIC])
@pytest.mark.parametrize("budget", [None, 9000])
def test_home_command_resolves_profile_after_durable_roundtrip(
    tmp_path, monkeypatch, profile, budget,
):
    monkeypatch.setenv("RESEARCH_DEFAULT_PROFILE", profile.name)
    repository, agents, commands, worker, _ = _stack(tmp_path)
    agents.context_builder.vector_retriever = Mock()
    agents.context_builder.vector_retriever.retrieve.return_value = []
    memory = repository.memory_repository
    project = memory.create_project(name="Profile", goal="", domain="research", metadata={})
    memory.replace_project_knowledge_scopes(
        project.id, ["command-papers"], expected_project_revision=project.revision,
    )
    command = commands.submit(ResearchCommandSubmission.model_validate({
        "mode": "project_run", "instruction": "Study durable research profiles",
        "project_id": project.id, "task": {"kind": "new"},
        **({"token_budget": budget} if budget is not None else {}),
    }), idempotency_key="default-profile-command")
    assert worker.run_once()
    result = commands.get(command.id)
    assert result.status == "queued", result.error
    run = agents.get_run(result.target_resource_id)
    assert run.options.workflow == profile.workflow
    assert run.token_budget == (budget or profile.token_budget)
    package = agents.context_builder.snapshot_repository.get(result.snapshot_id)
    assert package.reading_format == ("inline-v1" if profile == STANDARD else "legacy")
