from fastapi.testclient import TestClient

from app.api.main import create_app
from app.knowledge.repository import KnowledgeRepository
from app.memory.service import MemoryService


def test_memory_api_uses_explicit_workspace_routes_and_reviewed_commits(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = MemoryService(repository.memory_repository)
    collection = repository.create_collection("Memory API Scope")
    app = create_app(knowledge_repository=repository, memory_service=memory)

    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            json={"name": "API Project", "goal": "Validate Memory API", "domain": "testing"},
        )
        assert created.status_code == 201
        project = created.json()

        missing_revision = client.patch(
            f"/api/projects/{project['id']}", json={"goal": "must fail"}
        )
        task = client.post(
            f"/api/projects/{project['id']}/workspace-tasks",
            json={"title": "Review proposal", "goal": "Confirm memory update", "priority": "high"},
        )
        scopes = client.put(
            f"/api/projects/{project['id']}/knowledge-scopes",
            json={
                "expected_project_revision": project["revision"],
                "collection_slugs": [collection.slug],
            },
        )
        stale_scopes = client.put(
            f"/api/projects/{project['id']}/knowledge-scopes",
            json={
                "expected_project_revision": project["revision"],
                "collection_slugs": [collection.slug],
            },
        )
        terminal_decision = client.post(
            f"/api/projects/{project['id']}/decisions",
            json={"summary": "Invalid terminal decision", "status": "rejected"},
        )
        terminal_artifact = client.post(
            f"/api/projects/{project['id']}/artifacts",
            json={"type": "note", "reference": "memory://terminal", "status": "archived"},
        )
        proposal = client.post(
            f"/api/projects/{project['id']}/memory-proposals",
            json={
                "task_id": task.json()["id"],
                "rationale": "A human must confirm the decision.",
                "payload": {
                    "proposal_type": "decision_create",
                    "summary": "Use reviewed MemoryProposal commits",
                    "rationale": "Prevents automatic memory mutation",
                    "impact": "Safe future Agent boundary",
                },
            },
        )
        assert proposal.status_code == 201
        reviewed = client.post(
            f"/api/memory-proposals/{proposal.json()['id']}/review",
            json={"expected_revision": 1, "status": "approved", "review_note": "approved"},
        )
        committed = client.post(f"/api/memory-proposals/{proposal.json()['id']}/commit")
        repeated = client.post(f"/api/memory-proposals/{proposal.json()['id']}/commit")
        snapshot = client.get(f"/api/projects/{project['id']}/memory")
        ambiguous_task = client.get("/api/tasks/not-a-workspace-task")

    assert missing_revision.status_code == 422
    assert task.status_code == 201
    assert scopes.status_code == 200
    scope_body = scopes.json()
    assert len(scope_body) == 1
    assert scope_body[0]["project_id"] == project["id"]
    assert scope_body[0]["collection_slug"] == collection.slug
    assert stale_scopes.status_code == 409
    assert terminal_decision.status_code == 422
    assert terminal_artifact.status_code == 422
    assert reviewed.status_code == 200
    assert committed.status_code == 200
    assert committed.json()["proposal"]["status"] == "committed"
    assert committed.json()["record"]["status"] == "accepted"
    assert repeated.json()["record"]["id"] == committed.json()["record"]["id"]
    assert len(snapshot.json()["workspace_tasks"]) == 1
    assert len(snapshot.json()["decisions"]) == 1
    assert ambiguous_task.status_code == 404
