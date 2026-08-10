"""Adapters that drive existing platform services for Evaluation cases."""

from __future__ import annotations

from typing import Any

from app.agent.models import AgentRunCreateRequest
from app.benchmarking.contracts import EvaluationCase
from app.benchmarking.fixtures import EvaluationFixture
from app.context.models import ContextBuildRequest, runtime_context_payload


def execute_case(fixture: EvaluationFixture, case: EvaluationCase) -> dict[str, Any]:
    """Execute a declarative case through production services only.

    Fixture construction may establish reviewed prerequisites.  From this point
    onward no evaluator calls private Runtime nodes or writes business rows.
    """

    try:
        if case.scenario == "retrieval_allowed":
            return _retrieval(fixture, include_denied=False)
        if case.scenario == "retrieval_scope_filter":
            return _retrieval(fixture, include_denied=True)
        if case.scenario == "context_scoped":
            return _context(fixture)
        if case.scenario == "context_repeatable":
            return _context_repeatable(fixture)
        if case.scenario == "context_no_scope":
            return _context(fixture)
        if case.scenario == "research_success":
            return _research(fixture, propose=True)
        if case.scenario == "research_repair":
            return _research(fixture, propose=False)
        if case.scenario == "research_needs_review":
            return _research(fixture, propose=True)
        if case.scenario == "research_recovery":
            return _research_recovery(fixture)
        if case.scenario == "game_success":
            return _game(fixture)
        if case.scenario == "game_patch_mismatch":
            return _game(fixture)
        if case.scenario == "plugin_boundary":
            return _plugin_boundary(fixture)
        if case.scenario == "workspace_artifact":
            return _workspace_artifact(fixture)
        raise ValueError(f"Unknown Evaluation scenario: {case.scenario}")
    except Exception as exc:
        return {
            "status": "exception",
            "exception": f"{type(exc).__name__}: {exc}",
            "business_counts": _business_counts(fixture),
        }


def _retrieval(fixture: EvaluationFixture, *, include_denied: bool) -> dict[str, Any]:
    if fixture.query_service is None:
        raise AssertionError("Retrieval scenario requires the fixture query service")
    topics = [fixture.aliases["allowed_collection"]]
    response = fixture.query_service.search("fixture evidence", topic_slugs=topics, top_k=5)
    return {
        "status": "completed",
        "response": response,
        "include_denied": include_denied,
        "business_counts": _business_counts(fixture),
    }


def _context(fixture: EvaluationFixture) -> dict[str, Any]:
    package = fixture.context_builder.build_context(
        ContextBuildRequest(
            project_id=fixture.aliases["project_id"],
            task_id=fixture.aliases["task_id"],
            max_tokens=6000,
        )
    )
    return {
        "status": "completed",
        "package": package,
        "business_counts": _business_counts(fixture),
    }


def _context_repeatable(fixture: EvaluationFixture) -> dict[str, Any]:
    request = ContextBuildRequest(
        project_id=fixture.aliases["project_id"],
        task_id=fixture.aliases["task_id"],
        max_tokens=6000,
    )
    first = fixture.context_builder.preview_context(request)
    second = fixture.context_builder.preview_context(request)
    return {
        "status": "completed",
        "package": first,
        "repeat_package": second,
        "payloads": [runtime_context_payload(first), runtime_context_payload(second)],
        "business_counts": _business_counts(fixture),
    }


def _research(fixture: EvaluationFixture, *, propose: bool) -> dict[str, Any]:
    package = fixture.context_builder.build_context(
        ContextBuildRequest(
            project_id=fixture.aliases["project_id"], task_id=fixture.aliases["task_id"]
        )
    )
    run = fixture.agent_service.create_run(
        fixture.aliases["project_id"],
        fixture.aliases["task_id"],
        AgentRunCreateRequest(
            workflow="research",
            context_snapshot_id=package.snapshot_id,
            create_memory_proposal=propose,
            max_steps=10,
            max_tool_calls=1,
            token_budget=6000,
        ),
    )
    fixture.worker.run_once()
    final = fixture.agent_service.get_run(run.id)
    return _run_outcome(fixture, final.id)


def _research_recovery(fixture: EvaluationFixture) -> dict[str, Any]:
    package = fixture.context_builder.build_context(
        ContextBuildRequest(
            project_id=fixture.aliases["project_id"], task_id=fixture.aliases["task_id"]
        )
    )
    run = fixture.agent_service.create_run(
        fixture.aliases["project_id"],
        fixture.aliases["task_id"],
        AgentRunCreateRequest(
            workflow="research",
            context_snapshot_id=package.snapshot_id,
            max_steps=10,
            max_tool_calls=1,
            token_budget=6000,
        ),
    )
    fixture.worker.run_once()
    interrupted = fixture.agent_service.get_run(run.id)
    tool_calls_before = len(fixture.agent_service.repository.list_tool_calls(run.id))
    fixture.agent_service.resume_run(run.id)
    fixture.worker.run_once()
    outcome = _run_outcome(fixture, run.id)
    outcome.update(
        {
            "interrupted_status": interrupted.status,
            "tool_calls_before_resume": tool_calls_before,
        }
    )
    return outcome


def _game(fixture: EvaluationFixture) -> dict[str, Any]:
    package = fixture.context_builder.build_context(
        ContextBuildRequest(
            project_id=fixture.aliases["project_id"], task_id=fixture.aliases["task_id"]
        )
    )
    run = fixture.agent_service.create_run(
        fixture.aliases["project_id"],
        fixture.aliases["task_id"],
        AgentRunCreateRequest(
            workflow="model",
            context_snapshot_id=package.snapshot_id,
            create_memory_proposal=True,
            max_steps=3,
            max_tool_calls=1,
            token_budget=6000,
        ),
    )
    fixture.worker.run_once()
    return _run_outcome(fixture, run.id)


def _plugin_boundary(fixture: EvaluationFixture) -> dict[str, Any]:
    port = fixture.agent_service.domain_runtime_port()
    forbidden = [
        method
        for method in ("llm", "complete_foundation_output", "begin_repair", "mark_needs_review")
        if hasattr(port, method)
    ]
    manifests = fixture.agent_service.plugin_registry.list_manifests()
    return {
        "status": "completed",
        "forbidden_port_members": forbidden,
        "plugin_keys": [manifest.key for manifest in manifests],
        "business_counts": _business_counts(fixture),
    }


def _workspace_artifact(fixture: EvaluationFixture) -> dict[str, Any]:
    outcome = _research(fixture, propose=True)
    if outcome["status"] != "completed" or outcome.get("artifact_id") is None:
        return outcome
    projection = fixture.workspace.get_artifact_content(outcome["artifact_id"])
    outcome["projection"] = projection
    return outcome


def _run_outcome(fixture: EvaluationFixture, run_id: str) -> dict[str, Any]:
    run = fixture.agent_service.get_run(run_id)
    try:
        output = fixture.agent_service.repository.get_output(run_id)
    except KeyError:
        output = None
    tool_calls = fixture.agent_service.repository.list_tool_calls(run_id)
    artifact_id = output.structured.get("artifact_id") if output is not None else None
    proposal_id = output.structured.get("memory_proposal_id") if output is not None else None
    return {
        "status": run.status,
        "run": run,
        "output": output,
        "tool_calls": tool_calls,
        "artifact_id": artifact_id,
        "proposal_id": proposal_id,
        "business_counts": _business_counts(fixture),
    }


def _business_counts(fixture: EvaluationFixture) -> dict[str, int]:
    with fixture.repository._connect() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("agent_run_outputs", "artifacts", "memory_proposals")
        }
