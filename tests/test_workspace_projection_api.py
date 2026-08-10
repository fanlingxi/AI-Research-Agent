"""Regression coverage for the bounded Phase 4B Workspace UI projections."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.agent.service import AgentRunService
from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan, PublishedEntity
from tests.core_fixtures import persist_evidence_chunk


def _workspace_stack(tmp_path):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        agent_checkpoint_path=str(tmp_path / "agent_checkpoints.db"),
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    collection = repository.create_collection("Workspace API Scope")
    ingestion = repository.create_ingestion(
        collection=collection.slug,
        sources=["workspace-api-fixture.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="workspace-api-paper",
        chunk_id="workspace-api-paper:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="A ContextSnapshot keeps claims connected to locatable evidence.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    entity = PublishedEntity(
        id="workspace-api-entity",
        name="ContextSnapshot Evidence",
        type="Method",
        summary="A snapshot preserves a verified claim and evidence provenance chain.",
        evidence=[evidence],
        topic_slugs=[collection.slug],
        collection_slugs=[collection.slug],
    )
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO published_entities (
                id, normalized_name, entity_type, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (entity.id, entity.name.casefold(), entity.type, entity.model_dump_json()),
        )
        connection.execute(
            """
            INSERT INTO collection_memberships
                (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
            VALUES ('entity', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (entity.id, ingestion.id, collection.slug),
        )
        repository.core_repository.synchronize_published_entity_tx(connection, entity)

    memory = repository.memory_repository
    project = memory.create_project(
        name="Workspace API Project",
        goal="Exercise bounded Workspace UI projections.",
        domain="testing",
        metadata={},
    )
    memory.replace_project_knowledge_scopes(
        project.id, [collection.slug], expected_project_revision=project.revision
    )
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Inspect projected context",
        goal="Read only evidence selected by the immutable snapshot.",
        priority="high",
        metadata={},
    )
    agents = AgentRunService(repository, settings=settings)
    return repository, agents, project, task, evidence


def test_workspace_api_supports_context_run_trace_artifact_and_citation_flow(tmp_path) -> None:
    repository, agents, project, task, evidence = _workspace_stack(tmp_path)
    app = create_app(knowledge_repository=repository, agent_run_service=agents)

    with TestClient(app) as client:
        dashboard = client.get("/api/v1/workspace/dashboard?limit=5")
        unversioned_dashboard = client.get("/api/workspace/dashboard?limit=5")
        preview = client.post(
            f"/api/v1/projects/{project.id}/workspace-tasks/{task.id}/context-snapshots/preview",
            json={"max_tokens": 6000},
        )
        snapshot_response = client.post(
            f"/api/v1/projects/{project.id}/workspace-tasks/{task.id}/context-snapshots",
            json={"max_tokens": 6000},
        )
        snapshot = snapshot_response.json()
        snapshot_id = snapshot["id"]
        summary = client.get(f"/api/v1/context-snapshots/{snapshot_id}")
        first_items = client.get(f"/api/v1/context-snapshots/{snapshot_id}/items?limit=1")
        citation = client.get(
            f"/api/v1/context-snapshots/{snapshot_id}/evidence/{evidence.chunk_id}"
        )

        run_response = client.post(
            f"/api/projects/{project.id}/workspace-tasks/{task.id}/agent-runs",
            json={
                "workflow": "foundation",
                "context_snapshot_id": snapshot_id,
                "max_steps": 3,
                "max_tool_calls": 0,
                "token_budget": 6000,
            },
        )
        run = run_response.json()
        history = client.get(
            f"/api/v1/projects/{project.id}/workspace-tasks/{task.id}/agent-runs?limit=1"
        )
        project_history = client.get(f"/api/v1/projects/{project.id}/agent-runs?limit=1")
        invalid_cursor = client.get(
            f"/api/v1/projects/{project.id}/workspace-tasks/{task.id}/agent-runs?cursor=not-a-cursor"
        )
        first_trace = client.get(f"/api/v1/agent-runs/{run['id']}/trace?limit=1")
        second_trace = client.get(
            f"/api/v1/agent-runs/{run['id']}/trace?after_sequence=1&limit=1"
        )

        agents.mark_node(run["id"], status="preparing", node_name="load_context", input_summary={})
        agents.mark_node(run["id"], status="running", node_name="inspect_context", input_summary={})
        agents.mark_node(run["id"], status="validating", node_name="complete", input_summary={})
        output = agents.complete_foundation_output(run["id"], tool_result={"safe": "summary"})
        artifact = repository.memory_repository.create_artifact(
            project_id=project.id,
            task_id=task.id,
            artifact_type="research_report",
            reference=f"agent-run-output:{output.id}",
            status="ready",
            metadata={
                "agent_run_id": run["id"],
                "context_snapshot_id": snapshot_id,
                "context_sha256": snapshot["package_sha256"],
            },
        )
        content = client.get(f"/api/v1/artifacts/{artifact.id}/content")
        missing_evidence = client.get(
            f"/api/v1/context-snapshots/{snapshot_id}/evidence/not-in-this-snapshot"
        )

    assert dashboard.status_code == 200
    assert dashboard.json()["active_projects"][0]["id"] == project.id
    assert unversioned_dashboard.status_code == 404
    assert preview.status_code == 200
    assert preview.json()["snapshot"]["persisted"] is False
    assert preview.json()["snapshot"]["id"] is None
    assert "knowledge" not in preview.json()["snapshot"]
    assert snapshot_response.status_code == 201
    assert snapshot["persisted"] is True
    assert snapshot["item_counts"]["knowledge_claim_bundles"] == 1
    assert summary.status_code == 200
    assert summary.json()["package_sha256"] == snapshot["package_sha256"]
    assert first_items.status_code == 200
    assert first_items.json()["next_offset"] == 1
    assert citation.status_code == 404
    # Evidence IDs, not Chunk IDs, are the only valid citation lookup identifiers.
    assert run_response.status_code == 202
    assert history.status_code == 200
    assert history.json()["items"][0]["id"] == run["id"]
    assert project_history.status_code == 200
    assert project_history.json()["items"][0]["id"] == run["id"]
    assert invalid_cursor.status_code == 422
    assert first_trace.status_code == 200
    assert len(first_trace.json()["events"]) == 1
    assert first_trace.json()["next_event_sequence"] == 1
    assert second_trace.status_code == 200
    assert second_trace.json()["events"][0]["sequence"] == 2
    assert content.status_code == 200
    assert content.json()["rendered_markdown"].startswith("Phase 3A runtime foundation")
    assert content.json()["context_snapshot_id"] == snapshot_id
    assert missing_evidence.status_code == 404


def test_workspace_api_evidence_is_snapshot_bound_and_artifact_references_fail_closed(
    tmp_path,
) -> None:
    repository, agents, project, task, evidence = _workspace_stack(tmp_path)
    app = create_app(knowledge_repository=repository, agent_run_service=agents)

    with TestClient(app) as client:
        snapshot_response = client.post(
            f"/api/v1/projects/{project.id}/workspace-tasks/{task.id}/context-snapshots",
            json={"max_tokens": 6000},
        )
        snapshot_id = snapshot_response.json()["id"]
        items = client.get(f"/api/v1/context-snapshots/{snapshot_id}/items?limit=100")
        evidence_id = next(
            item["item_id"]
            for item in items.json()["items"]
            if item["item_type"] == "evidence"
        )
        citation = client.get(
            f"/api/v1/context-snapshots/{snapshot_id}/evidence/{evidence_id}"
        )
        unrelated = repository.memory_repository.create_artifact(
            project_id=project.id,
            task_id=task.id,
            artifact_type="note",
            reference="memory://not-an-agent-output",
            status="ready",
            metadata={},
        )
        unavailable_content = client.get(f"/api/v1/artifacts/{unrelated.id}/content")

    assert snapshot_response.status_code == 201
    assert citation.status_code == 200
    assert citation.json()["evidence"]["id"] == evidence_id
    assert citation.json()["evidence"]["quote"] == evidence.quote
    assert citation.json()["claim"]["id"]
    assert citation.json()["chunk"]["id"] == evidence.chunk_id
    assert "content" not in citation.json()["chunk"]
    assert unavailable_content.status_code == 409
