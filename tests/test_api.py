from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.task_store import ResearchTaskStore
from app.schemas.quality import EvaluationResult


def test_api_health_and_missing_task() -> None:
    with TestClient(create_app(ResearchTaskStore())) as client:
        health = client.get("/health")
        missing = client.get("/api/tasks/does-not-exist")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert missing.status_code == 404


def test_api_runs_a_background_research_task(monkeypatch) -> None:
    def fake_workflow(**kwargs):
        return {
            "run_id": "run-api-test",
            "query": kwargs["query"],
            "final_report": "# API Report",
            "evaluation_result": EvaluationResult(
                overall_score=0.9,
                passed=True,
                summary="ok",
                evidence_status="formal",
                evidence_admissible=True,
            ),
            "graph_entities": [],
            "graph_relations": [],
            "graph_paths": [],
        }

    monkeypatch.setattr("app.api.main.run_research_workflow", fake_workflow)
    with TestClient(create_app(ResearchTaskStore())) as client:
        submitted = client.post("/api/research", json={"query": "GraphRAG API test"})
        task_id = submitted.json()["id"]
        task = client.get(f"/api/tasks/{task_id}")
        report = client.get(f"/api/tasks/{task_id}/report")

    assert submitted.status_code == 202
    assert task.json()["status"] == "completed"
    assert task.json()["evaluation"]["evidence_admissible"]
    assert report.json()["report"] == "# API Report"
