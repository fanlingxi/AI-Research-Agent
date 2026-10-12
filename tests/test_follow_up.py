import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.llms.provider import MockLLMClient
from app.research_commands.service import ResearchCommandService
from tests.test_research_workflow import (
    _analysis,
    _draft,
    _plan,
    _research_request,
    _research_stack,
)


@pytest.fixture
def stack(tmp_path):
    repo, service, worker, project, task, package, bundle, llm = _research_stack(tmp_path, [])
    llm.responses = [
        json.dumps(item)
        for item in (
            _analysis(),
            _plan(),
            _draft(
                bundle.claim.claim_id, bundle.evidence[0].evidence_id, valid=True, proposal=False
            ),
        )
    ]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    commands = ResearchCommandService(
        repo, report_service=worker.report_service, agent_service=service
    )
    worker.research_command_service = commands
    app = create_app(
        knowledge_repository=repo,
        agent_run_service=service,
        report_service=worker.report_service,
        research_command_service=commands,
    )
    with TestClient(app) as client:
        yield repo, service, worker, project, task, run, client


@pytest.mark.parametrize("question", ["它与 RAG 相比有什么局限？", ""])
def test_draft_uses_bounded_report_and_one_call_without_transaction(stack, question):
    repo, service, _, _, task, run, client = stack
    original = service.repository.get_output(run.id).model_dump()
    answer = {
        "standalone_question": "Governed ContextSnapshot 与 RAG 相比有什么局限？",
        "suggestions": [
            "Governed ContextSnapshot 如何核验引用？",
            "RAG 如何处理过时证据？",
            "如何评估 RAG 的召回率？",
        ],
    }

    def invoke(prompt, system_prompt):
        # A write lock can be acquired while the model is running.
        with repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        context = json.loads(prompt)
        assert context["父任务标题"] == task.title
        assert context["父任务目标"] == task.goal
        assert len(context["报告结论要点"]) <= 6000
        assert "assertion" in context["报告结论要点"]
        assert "保留" in system_prompt
        return json.dumps(answer)

    service.llm = Mock(invoke=Mock(side_effect=invoke))
    response = client.post(f"/api/agent-runs/{run.id}/follow-up-draft", json={"question": question})
    assert response.status_code == 200
    assert response.json() == {
        **answer,
        "standalone_question": answer["standalone_question"] if question else "",
        "fallback": False,
    }
    service.llm.invoke.assert_called_once()
    assert service.repository.get_output(run.id).model_dump() == original


@pytest.mark.parametrize("failure", ["mock", "exception", "invalid_json", "invalid_shape"])
def test_draft_falls_back(stack, failure):
    _, service, _, _, task, run, client = stack
    if failure == "mock":
        service.llm = MockLLMClient()
    else:
        service.llm = Mock()
        if failure == "exception":
            service.llm.invoke.side_effect = RuntimeError("unavailable")
        else:
            service.llm.invoke.return_value = "not json" if failure == "invalid_json" else "{}"
    for question in ("还有哪些局限？", ""):
        response = client.post(
            f"/api/agent-runs/{run.id}/follow-up-draft", json={"question": question}
        )
        assert response.status_code == 200
        assert response.json() == {
            "standalone_question": f"关于「{task.title}」：{question}" if question else "",
            "suggestions": [],
            "fallback": True,
        }


def test_draft_missing_unfinished_and_reportless_runs(stack):
    repo, _, _, _, _, run, client = stack
    assert client.post("/api/agent-runs/missing/follow-up-draft", json={}).status_code == 404
    assert client.get("/api/agent-runs/missing/follow-ups").status_code == 404
    with repo._connect() as connection:
        connection.execute("UPDATE agent_runs SET status = 'running' WHERE id = ?", (run.id,))
    assert client.post(f"/api/agent-runs/{run.id}/follow-up-draft", json={}).status_code == 409
    with repo._connect() as connection:
        connection.execute("UPDATE agent_runs SET status = 'completed' WHERE id = ?", (run.id,))
        connection.execute("DELETE FROM agent_run_outputs WHERE run_id = ?", (run.id,))
    assert client.post(f"/api/agent-runs/{run.id}/follow-up-draft", json={}).status_code == 409


@pytest.mark.parametrize("with_artifact", [True, False])
def test_command_creates_linked_new_task_snapshot_and_lists_latest_run(stack, with_artifact):
    repo, service, worker, project, task, parent, client = stack
    parent_output = service.repository.get_output(parent.id).model_dump()
    parent_snapshot = service.load_context_snapshot(parent.id).model_dump()
    artifact = service.follow_up.parent_report(parent.id)[1]
    repo.memory_repository.create_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="report",
        reference="unrelated-report",
        metadata={"agent_run_id": parent.id},
        status="ready",
    )
    payload = {
        "mode": "project_run",
        "project_id": project.id,
        "task": {"kind": "new"},
        "instruction": "Governed ContextSnapshot 与 RAG 相比有什么局限？",
        "follow_up_of": {
            "run_id": parent.id,
            "artifact_id": artifact.id if with_artifact else None,
        },
    }
    headers = {"Idempotency-Key": "follow-up-test-key"}
    response = client.post("/api/v1/research-commands", json=payload, headers=headers)
    assert response.status_code == 202
    assert worker.run_once()
    command = client.get(f"/api/v1/research-commands/{response.json()['id']}").json()
    assert command["status"] == "queued", command
    child = repo.memory_repository.get_workspace_task(command["task_id"])
    assert child.goal == payload["instruction"]
    assert child.metadata["follow_up_of"] == {**payload["follow_up_of"], "parent_task_id": task.id}
    assert child.id != task.id
    assert command["snapshot_id"] != parent.context_snapshot_id
    replay = client.post("/api/v1/research-commands", json=payload, headers=headers)
    assert replay.json()["target_resource_id"] == command["target_resource_id"]
    children = client.get(f"/api/agent-runs/{parent.id}/follow-ups").json()
    assert len(children) == 1
    assert children[0]["latest_run"]["id"] == command["target_resource_id"]
    assert children[0]["artifact"] is None

    # Finish a later run for the child task, then confirm the latest report is selected.
    service.cancel_run(command["target_resource_id"])
    latest = service.create_run(project.id, child.id, _research_request(command["snapshot_id"]))
    bundle = service.load_context_snapshot(latest.id).knowledge.claim_bundles[0]
    service.llm.responses = [
        json.dumps(item)
        for item in (
            _analysis(),
            _plan(),
            _draft(
                bundle.claim.claim_id, bundle.evidence[0].evidence_id, valid=True, proposal=False
            ),
        )
    ]
    for _ in range(3):
        if service.get_run(latest.id).status == "completed":
            break
        assert worker.run_once()
    assert service.get_run(latest.id).status == "completed"
    child_artifact = service.follow_up.parent_report(latest.id)[1]
    repo.memory_repository.create_artifact(
        project_id=project.id,
        task_id=child.id,
        artifact_type="report",
        reference="unrelated-child-report",
        metadata={"agent_run_id": latest.id},
        status="ready",
    )
    children = client.get(f"/api/agent-runs/{parent.id}/follow-ups").json()
    assert children[0]["latest_run"]["id"] == latest.id
    assert children[0]["artifact"]["id"] == child_artifact.id
    assert client.get(f"/api/agent-runs/{latest.id}/follow-ups").json() == []
    assert service.repository.get_output(parent.id).model_dump() == parent_output
    assert service.load_context_snapshot(parent.id).model_dump() == parent_snapshot


def test_command_rejects_cross_project_and_mismatched_artifact_before_creation(stack):
    repo, _, _, project, task, parent, client = stack
    other = repo.memory_repository.create_project(
        name="另一个项目", goal="", domain="research", metadata={}
    )
    payload = {
        "mode": "project_run",
        "project_id": other.id,
        "task": {"kind": "new"},
        "instruction": "独立研究问题",
        "follow_up_of": {"run_id": parent.id},
    }
    headers = {"Idempotency-Key": "bad-follow-up-key"}
    assert (
        client.post("/api/v1/research-commands", json=payload, headers=headers).status_code == 409
    )
    payload["project_id"] = project.id
    payload["follow_up_of"]["artifact_id"] = "unrelated-report"
    assert (
        client.post("/api/v1/research-commands", json=payload, headers=headers).status_code == 409
    )
    payload["task"] = {"kind": "existing", "task_id": task.id}
    assert (
        client.post("/api/v1/research-commands", json=payload, headers=headers).status_code == 422
    )
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM research_commands").fetchone()[0] == 0
