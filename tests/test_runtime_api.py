from __future__ import annotations

from fastapi.testclient import TestClient

from app.agent.models import AgentRunCreateRequest
from app.agent.service import AgentRunService
from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateDecision, CandidateEntity, EvidenceSpan
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.runtime.service import RuntimeObservabilityService
from tests.core_fixtures import persist_evidence_chunk


def _stack(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(vault),
        qdrant_url="http://qdrant.invalid:6333",
        neo4j_uri="bolt://neo4j.invalid:7687",
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    ingestion_service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(str(vault)),
        require_live_llm=False,
    )
    agent_service = AgentRunService(repository, settings=settings)
    runtime = RuntimeObservabilityService(
        repository,
        settings,
        llm_provider="fixture",
        live_llm_configured=True,
        connection_probe=lambda endpoint: endpoint.startswith("http://qdrant"),
    )
    app = create_app(
        knowledge_repository=repository,
        knowledge_service=ingestion_service,
        agent_run_service=agent_service,
        runtime_observability_service=runtime,
    )
    return repository, ingestion_service, agent_service, app


def _failed_projection(repository, service) -> str:
    ingestion = repository.create_ingestion(
        topic="Runtime 投影", sources=["paper.pdf"], pdf_max_pages=3, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="paper:runtime",
        chunk_id="paper:runtime:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Runtime exposes a failed projection with a safe retry action.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    repository.add_candidate_entity(
        CandidateEntity(
            id="candidate-runtime-projection",
            ingestion_id=ingestion.id,
            topic_slug=ingestion.topic_slug,
            name="Runtime Projection",
            type="Concept",
            summary="Runtime 页展示失败投影并允许仅重试该事件。",
            confidence=0.95,
            evidence=evidence,
        )
    )
    service.decide("candidate-runtime-projection", CandidateDecision(decision="approve"))
    event = repository.claim_projection(lease_seconds=30, owner_id="failed-worker")
    assert event is not None
    repository.fail_projection(
        event.id,
        "neo4j endpoint missing",
        expected_attempt=event.attempts,
        expected_owner=event.lease_owner,
    )
    return event.id


def test_runtime_overview_exposes_services_counts_and_worker_heartbeat(tmp_path) -> None:
    repository, _, _, app = _stack(tmp_path)
    ingestion = repository.create_ingestion(
        topic="Runtime Queue", sources=["paper.pdf"], pdf_max_pages=3, enqueue=True
    )
    repository.jobs.upsert_executor_heartbeat(
        "worker-runtime",
        role="worker",
        version="test-v1",
        started_at=ingestion.created_at,
        current_job_id=None,
        metadata={"concurrency": 1},
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/runtime/overview")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload["services"]) == {"api", "worker", "llm", "qdrant", "neo4j", "vault"}
    assert payload["services"]["worker"]["available"]
    assert payload["services"]["qdrant"]["available"]
    assert not payload["services"]["neo4j"]["available"]
    assert payload["work_counts"]["ingestion"]["queued"] == 1
    assert payload["executors"][0]["current_job_id"] is None


def test_runtime_work_unifies_kinds_filters_and_uses_cursor_pagination(tmp_path) -> None:
    repository, _, agents, app = _stack(tmp_path)
    ingestion = repository.create_ingestion(
        topic="Runtime Ingestion", sources=["paper.pdf"], pdf_max_pages=3, enqueue=True
    )
    report = repository.reports.create_report(
        query="Runtime report status?",
        topic_slugs=[],
        top_k=8,
        report_depth="standard",
    )
    project = repository.memory_repository.create_project(
        name="Runtime Project", goal="", domain="", metadata={}
    )
    task = repository.memory_repository.create_workspace_task(
        project_id=project.id,
        title="Runtime Agent Task",
        goal="",
        priority="normal",
        metadata={},
    )
    run = agents.create_run(project.id, task.id, AgentRunCreateRequest(workflow="foundation"))

    with TestClient(app) as client:
        first = client.get("/api/v1/runtime/work?limit=2")
        second = client.get(
            "/api/v1/runtime/work",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )
        reports = client.get("/api/v1/runtime/work?kind=report&status=queued")
        invalid = client.get("/api/v1/runtime/work?cursor=not-a-cursor")

    assert first.status_code == 200 and second.status_code == 200
    combined = first.json()["items"] + second.json()["items"]
    assert {item["kind"] for item in combined} == {"ingestion", "report", "agent_run"}
    assert {item["resource_id"] for item in combined} == {ingestion.id, report.id, run.id}
    assert all(item["queue_position"] is not None for item in combined)
    assert reports.json()["items"][0]["detail_route"].endswith(report.id)
    assert invalid.status_code == 422


def test_runtime_projection_retry_is_targeted_and_rejects_second_retry(tmp_path) -> None:
    repository, service, _, app = _stack(tmp_path)
    event_id = _failed_projection(repository, service)

    with TestClient(app) as client:
        before = client.get("/api/v1/runtime/work?kind=projection&status=failed")
        retried = client.post(f"/api/v1/runtime/projections/{event_id}/retry")
        replay = client.post(f"/api/v1/runtime/projections/{event_id}/retry")

    assert before.json()["items"][0]["can_retry"]
    assert retried.status_code == 200
    assert retried.json()["id"] == event_id
    assert retried.json()["job_status"] == "queued"
    assert retried.json()["last_error"] is None
    assert replay.status_code == 409
