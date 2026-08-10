from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.errors import AgentRunConflictError
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.domain_plugins.game_modeling.knowledge import GameKnowledgeAuthoringService
from app.domain_plugins.game_modeling.models import (
    GameFormulaCandidateCreateRequest,
    GameModelProvenance,
    GamePatchCandidateCreateRequest,
)
from app.domain_plugins.game_modeling.plugin import (
    GAME_MODELING_CHECKPOINT_NAMESPACE,
    GAME_MODELING_PLUGIN_KEY,
)
from app.domain_plugins.game_modeling.tools import (
    _require_source_version,
    build_game_model_tool_registry,
)
from app.domain_plugins.models import (
    ProjectDomainPluginUpdateRequest,
    WorkspaceTaskPluginBindRequest,
)
from app.domain_plugins.registry import create_builtin_plugin_registry
from app.domain_plugins.service import DomainPluginService
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan
from app.knowledge.service import KnowledgeIngestionService
from app.llms.provider import MockLLMClient
from app.worker import KnowledgeWorker
from tests.core_fixtures import persist_evidence_chunk

_PATCH_VERSION = "1.2.0"
_FORMULA_EXPRESSION = "base_damage + attack * ratio"


def test_game_knowledge_authoring_uses_candidate_review_and_immutable_source_versions(
    tmp_path,
) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Game Authoring")
    ingestion = repository.create_ingestion(
        collection=collection.name,
        sources=["patch-notes.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    evidence = EvidenceSpan(
        paper_id="paper:patch-notes",
        chunk_id="paper:patch-notes:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Patch 1.2.0 reviewed game balance evidence.",
    )
    persist_evidence_chunk(
        repository,
        ingestion_id=ingestion.id,
        evidence=evidence,
        source_version=_PATCH_VERSION,
    )
    authoring = GameKnowledgeAuthoringService(repository)
    candidate = authoring.create_patch_candidate(
        GamePatchCandidateCreateRequest(
            ingestion_id=ingestion.id,
            name="Patch 1.2.0",
            summary="Reviewed game patch version 1.2.0 for Formula calculations.",
            evidence=evidence,
            patch={"patch_version": _PATCH_VERSION},
        )
    )

    # The generic review API cannot silently publish plugin-owned knowledge.
    with pytest.raises(ValueError, match="Domain Knowledge candidates"):
        repository.publish_entity(candidate.id)
    assert repository.get_candidate(candidate.id)["candidate"]["status"] == "draft"

    publication = authoring.approve_candidate(candidate.id)
    published = repository.get_candidate(candidate.id)
    entity = repository.core_repository.get_entity(publication.entity_id)
    claim = repository.core_repository.get_claim(publication.claim_id)
    assert published["candidate"]["status"] == "published"
    assert entity.entity_type == "Patch"
    assert entity.domain == "game"
    assert claim.claim_type == "game_patch_version"
    assert claim.properties["source_version"] == _PATCH_VERSION
    assert repository.core_repository.source_version_for_evidence(evidence) == _PATCH_VERSION
    with repository._connect() as connection:
        mapped = connection.execute(
            """
            SELECT 1 FROM collection_memberships AS membership
            JOIN legacy_record_map AS mapping
              ON mapping.legacy_table = 'published_entities'
             AND mapping.legacy_id = membership.aggregate_id
            WHERE membership.aggregate_type = 'entity'
              AND membership.collection_slug = ?
              AND mapping.core_table = 'entities'
              AND mapping.core_id = ?
            """,
            (collection.slug, publication.entity_id),
        ).fetchone()
    assert mapped is not None

    with pytest.raises(ValueError, match="exactly match"):
        authoring.create_patch_candidate(
            GamePatchCandidateCreateRequest(
                ingestion_id=ingestion.id,
                name="Patch 1.2.1",
                summary="A conflicting patch version using the same immutable evidence source.",
                evidence=evidence,
                patch={"patch_version": "1.2.1"},
            )
        )

    with pytest.raises(ValueError, match="exactly match"):
        _require_source_version(
            GameModelProvenance(
                claim_id="claim-patch",
                entity_id="entity-patch",
                evidence_ids=["evidence-a", "evidence-b"],
                source_ids=["source-a", "source-b"],
                chunk_ids=["chunk-a", "chunk-b"],
                source_versions=[_PATCH_VERSION, "1.2.1"],
            ),
            _PATCH_VERSION,
            "Patch",
        )


def test_game_knowledge_authoring_api_is_plugin_scoped(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    collection = repository.create_collection("Game API")
    ingestion = repository.create_ingestion(
        collection=collection.name,
        sources=["game-api.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    evidence = EvidenceSpan(
        paper_id="paper:game-api",
        chunk_id="paper:game-api:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Patch 1.2.0 evidence for the governed Game API publication test.",
    )
    persist_evidence_chunk(
        repository,
        ingestion_id=ingestion.id,
        evidence=evidence,
        source_version=_PATCH_VERSION,
    )
    payload = {
        "ingestion_id": ingestion.id,
        "name": "Patch 1.2.0 API",
        "summary": "Reviewed game patch version published through the plugin API.",
        "evidence": evidence.model_dump(),
        "patch": {"patch_version": _PATCH_VERSION},
    }
    with TestClient(create_app(knowledge_repository=repository)) as client:
        created = client.post(
            "/api/v1/domain-plugins/game_modeling/patch-candidates", json=payload
        )
        candidate_id = created.json()["id"]
        generic_approval = client.post(
            f"/api/knowledge/candidates/{candidate_id}/decision", json={"decision": "approve"}
        )
        generic_rejection = client.post(
            f"/api/knowledge/candidates/{candidate_id}/decision", json={"decision": "reject"}
        )
        approved = client.post(
            f"/api/v1/domain-plugins/game_modeling/candidates/{candidate_id}/approve"
        )
        replayed = client.post(
            f"/api/v1/domain-plugins/game_modeling/candidates/{candidate_id}/approve"
        )
        rejected_candidate = client.post(
            "/api/v1/domain-plugins/game_modeling/patch-candidates",
            json={
                **payload,
                "name": "Patch 1.2.0 rejected",
                "summary": "A reviewed Game patch candidate that is explicitly rejected.",
            },
        )
        rejected = client.post(
            "/api/v1/domain-plugins/game_modeling/candidates/"
            f"{rejected_candidate.json()['id']}/reject",
            json={"review_note": "Not selected for the current Game model."},
        )

    assert created.status_code == 201
    assert generic_approval.status_code == 409
    assert generic_rejection.status_code == 409
    assert approved.status_code == 200
    assert approved.json()["entity_type"] == "Patch"
    assert replayed.status_code == 409
    assert rejected_candidate.status_code == 201
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def _publish_game_fact(
    repository: KnowledgeRepository,
    *,
    collection_slug: str,
    legacy_id: str,
    name: str,
    entity_type: str,
    claim_object: dict[str, object],
    source_version: str,
) -> tuple[str, EvidenceSpan]:
    """Publish a reviewed Game fact through its supported public lifecycle."""

    ingestion = repository.create_ingestion(
        collection=collection_slug,
        sources=[f"{legacy_id}.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id=f"paper:{legacy_id}",
        chunk_id=f"paper:{legacy_id}:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote=f"Reviewed game data for {name} under patch {source_version}.",
    )
    persist_evidence_chunk(
        repository,
        ingestion_id=ingestion.id,
        evidence=evidence,
        title=name,
        source_version=source_version,
    )
    authoring = GameKnowledgeAuthoringService(repository)
    if entity_type == "Patch":
        candidate = authoring.create_patch_candidate(
            GamePatchCandidateCreateRequest(
                ingestion_id=ingestion.id,
                name=name,
                summary=f"Reviewed {entity_type} definition for {name}.",
                evidence=evidence,
                patch=claim_object,
            )
        )
    else:
        candidate = authoring.create_formula_candidate(
            GameFormulaCandidateCreateRequest(
                ingestion_id=ingestion.id,
                name=name,
                summary=f"Reviewed {entity_type} definition for {name}.",
                evidence=evidence,
                formula=claim_object,
            )
        )
    publication = authoring.approve_candidate(candidate.id)
    assert repository.core_repository.source_version_for_evidence(evidence) == source_version
    return publication.claim_id, evidence


def _game_stack(
    tmp_path: Path,
    *,
    formula_expression: str = _FORMULA_EXPRESSION,
    task_patch_version: str = _PATCH_VERSION,
    formula_in_scope: bool = True,
):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        agent_checkpoint_path=str(tmp_path / "agent_checkpoints.db"),
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    allowed = repository.create_collection("Game Allowed")
    denied = repository.create_collection("Game Denied")
    patch_claim_id, patch_evidence = _publish_game_fact(
        repository,
        collection_slug=allowed.slug,
        legacy_id="game-patch-1-2-0",
        name="Patch 1.2.0",
        entity_type="Patch",
        claim_object={"patch_version": _PATCH_VERSION},
        source_version=_PATCH_VERSION,
    )
    formula_claim_id, formula_evidence = _publish_game_fact(
        repository,
        collection_slug=allowed.slug if formula_in_scope else denied.slug,
        legacy_id="game-fireball-formula",
        name="Fireball Formula",
        entity_type="Formula",
        claim_object={
            "expression": formula_expression,
            "patch_claim_id": patch_claim_id,
            "patch_version": _PATCH_VERSION,
            "unit": "damage",
            "variables": ["base_damage", "attack", "ratio"],
        },
        source_version=_PATCH_VERSION,
    )

    memory = repository.memory_repository
    project = memory.create_project(
        name="Game Model Project",
        goal="Model a reviewed game formula without widening scope.",
        domain="game",
        metadata={},
    )
    memory.replace_project_knowledge_scopes(
        project.id, [allowed.slug], expected_project_revision=project.revision
    )
    project = memory.get_project(project.id)
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Model Fireball under Patch 1.2.0",
        goal="Calculate the reviewed Fireball formula for the selected patch.",
        priority="high",
        metadata={
            "expected_output": "A deterministic, evidence-cited game model result.",
            "game_model": {
                "formula_claim_id": formula_claim_id,
                "patch_claim_id": patch_claim_id,
                "patch_version": task_patch_version,
                "parameters": {"base_damage": 100, "attack": 50, "ratio": 1.5},
            },
        },
    )
    registry = create_builtin_plugin_registry()
    domains = DomainPluginService(memory, registry)
    project = domains.set_project_binding(
        project.id,
        GAME_MODELING_PLUGIN_KEY,
        ProjectDomainPluginUpdateRequest(expected_project_revision=project.revision),
    ).project
    task = domains.bind_workspace_task(
        task.id,
        WorkspaceTaskPluginBindRequest(
            expected_revision=task.revision,
            plugin_key=GAME_MODELING_PLUGIN_KEY,
        ),
    ).task
    builder = ContextBuilderService(repository)
    package = builder.build_context(
        ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=6000)
    )
    agents = AgentRunService(
        repository,
        context_builder=builder,
        settings=settings,
        llm=MockLLMClient(),
        plugin_registry=registry,
    )
    runtime = AgentRuntime(
        agents,
        checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(repository, settings=settings),
        report_service=KnowledgeReportService(
            repository, settings=settings, require_live_llm=False
        ),
        agent_runtime=runtime,
    )
    return {
        "repository": repository,
        "service": agents,
        "worker": worker,
        "project": project,
        "task": task,
        "package": package,
        "formula_claim_id": formula_claim_id,
        "patch_claim_id": patch_claim_id,
        "formula_evidence": formula_evidence,
        "patch_evidence": patch_evidence,
        "checkpoint_path": Path(settings.agent_checkpoint_path),
    }


def _model_request(snapshot_id: str, *, propose: bool = False) -> AgentRunCreateRequest:
    return AgentRunCreateRequest(
        workflow="model",
        context_snapshot_id=snapshot_id,
        create_memory_proposal=propose,
        max_steps=3,
        max_tool_calls=1,
        token_budget=6000,
    )


def test_game_modeling_plugin_runs_deterministically_with_auditable_finalization(tmp_path) -> None:
    stack = _game_stack(tmp_path)
    service = stack["service"]
    package = stack["package"]
    registry = build_game_model_tool_registry(package)
    arguments = {
        "context_snapshot_id": package.snapshot_id,
        "context_sha256": package.package_sha256,
    }
    first = registry.execute(
        name="game.run_formula", permission="deterministic_compute", arguments=arguments
    )
    second = registry.execute(
        name="game.run_formula", permission="deterministic_compute", arguments=arguments
    )
    assert first == second
    assert first["value"] == 175
    with pytest.raises(ValueError, match="exact ContextSnapshot identity"):
        registry.execute(
            name="game.run_formula",
            permission="deterministic_compute",
            arguments={"context_snapshot_id": "other", "context_sha256": "other"},
        )

    run = service.create_run(
        stack["project"].id,
        stack["task"].id,
        _model_request(package.snapshot_id, propose=True),
    )
    assert run.plugin.key == GAME_MODELING_PLUGIN_KEY
    assert run.plugin.workflow_key == "model"
    with pytest.raises(AgentRunConflictError, match="does not permit runtime_read"):
        service.record_tool_call(
            run.id,
            sequence=99,
            tool_name="forbidden.runtime_read",
            permission="runtime_read",
            arguments={},
            result_summary={},
            idempotency_key=f"{run.id}:forbidden",
        )
    assert stack["worker"].run_once()

    completed = service.get_run(run.id)
    output = service.repository.get_output(run.id)
    tool_call = service.repository.list_tool_calls(run.id)[0]
    artifact = stack["repository"].memory_repository.get_artifact(
        output.structured["artifact_id"]
    )
    proposal = stack["repository"].memory_repository.get_proposal(
        output.structured["memory_proposal_id"]
    )
    assert completed.status == "completed"
    assert output.output_type == "game_model_result"
    assert output.structured["game_model"]["value"] == 175
    assert output.validation["passed"] is True
    assert artifact.type == "game_model_report"
    assert artifact.status == "ready"
    assert artifact.reference == f"agent-run-output:{output.id}"
    assert proposal.status == "proposed"
    assert tool_call.tool_name == "game.run_formula"
    assert tool_call.permission == "deterministic_compute"
    assert "expression" not in json.dumps(tool_call.result_summary, ensure_ascii=False)
    assert "parameters" not in json.dumps(tool_call.result_summary, ensure_ascii=False)
    bundles = {
        bundle.claim.claim_id: bundle for bundle in package.knowledge.claim_bundles
    }
    expected_evidence_ids = {
        evidence.evidence_id
        for claim_id in (stack["formula_claim_id"], stack["patch_claim_id"])
        for evidence in bundles[claim_id].evidence
    }
    assert set(output.validation["cited_evidence_ids"]) == expected_evidence_ids

    checkpoint_path = stack["checkpoint_path"]
    with AgentCheckpointFactory(str(checkpoint_path)).open(
        namespace=GAME_MODELING_CHECKPOINT_NAMESPACE
    ) as checkpointer:
        saved = checkpointer.get_tuple(
            {
                "configurable": {
                    "thread_id": run.id,
                    "checkpoint_ns": GAME_MODELING_CHECKPOINT_NAMESPACE,
                }
            }
        )
    assert saved is not None
    values = saved.checkpoint["channel_values"]
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    assert "context_package" not in values
    assert "tool_results" not in values
    assert _FORMULA_EXPRESSION not in serialized
    assert _FORMULA_EXPRESSION.encode("utf-8") not in checkpoint_path.read_bytes()
    assert values["context_snapshot_id"] == package.snapshot_id
    assert values["selected_claim_ids"] == [
        stack["formula_claim_id"],
        stack["patch_claim_id"],
    ]
    with sqlite3.connect(checkpoint_path) as connection:
        namespaces = {
            row[0] for row in connection.execute("SELECT DISTINCT checkpoint_ns FROM checkpoints")
        }
    assert namespaces == {GAME_MODELING_CHECKPOINT_NAMESPACE}


def test_game_modeling_fails_closed_for_out_of_scope_formula_without_finalization(tmp_path) -> None:
    stack = _game_stack(tmp_path, formula_in_scope=False)
    assert {
        bundle.claim.claim_id for bundle in stack["package"].knowledge.claim_bundles
    } == {stack["patch_claim_id"]}
    run = stack["service"].create_run(
        stack["project"].id,
        stack["task"].id,
        _model_request(stack["package"].snapshot_id),
    )

    assert stack["worker"].run_once()
    failed = stack["service"].get_run(run.id)
    assert failed.status == "failed"
    assert "Formula Claim is absent" in (failed.error_message or "")
    assert stack["service"].repository.list_tool_calls(run.id) == []
    with stack["repository"]._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0


def test_game_modeling_rejects_unsafe_formula_without_side_effects(tmp_path) -> None:
    stack = _game_stack(
        tmp_path,
        formula_expression="__import__('pathlib').Path('forbidden').touch()",
    )
    run = stack["service"].create_run(
        stack["project"].id,
        stack["task"].id,
        _model_request(stack["package"].snapshot_id),
    )

    assert stack["worker"].run_once()
    failed = stack["service"].get_run(run.id)
    assert failed.status == "failed"
    assert "unsupported operation" in (failed.error_message or "")
    assert not (tmp_path / "forbidden").exists()
    with stack["repository"]._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_game_modeling_validates_patch_version_and_run_budgets(tmp_path) -> None:
    stack = _game_stack(tmp_path, task_patch_version="1.2.1")
    with pytest.raises(ValueError, match="tool call"):
        stack["service"].create_run(
            stack["project"].id,
            stack["task"].id,
            AgentRunCreateRequest(
                workflow="model",
                context_snapshot_id=stack["package"].snapshot_id,
                max_steps=3,
                max_tool_calls=0,
                token_budget=6000,
            ),
        )

    run = stack["service"].create_run(
        stack["project"].id,
        stack["task"].id,
        _model_request(stack["package"].snapshot_id),
    )
    assert stack["worker"].run_once()
    failed = stack["service"].get_run(run.id)
    assert failed.status == "failed"
    assert "patch_version" in (failed.error_message or "")


def test_game_modeling_resume_reuses_the_same_snapshot_tool_call(tmp_path, monkeypatch) -> None:
    stack = _game_stack(tmp_path)
    service = stack["service"]
    original_finalize = service.finalize_run
    failed_once = False

    def interrupt_finalization(run_id, *, command):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated finalization interruption")
        return original_finalize(run_id, command=command)

    monkeypatch.setattr(service, "finalize_run", interrupt_finalization)
    run = service.create_run(
        stack["project"].id,
        stack["task"].id,
        _model_request(stack["package"].snapshot_id),
    )

    assert stack["worker"].run_once()
    assert service.get_run(run.id).status == "failed"
    assert len(service.repository.list_tool_calls(run.id)) == 1
    with stack["repository"]._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0

    assert service.resume_run(run.id).status == "queued"
    assert stack["worker"].run_once()
    assert service.get_run(run.id).status == "completed"
    assert len(service.repository.list_tool_calls(run.id)) == 1
    with stack["repository"]._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
