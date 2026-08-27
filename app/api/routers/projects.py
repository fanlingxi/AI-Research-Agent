from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.errors import domain_plugin_call, game_knowledge_call, memory_call, workspace_call
from app.api.schemas import GameKnowledgeReviewRequest
from app.domain_plugins.game_modeling.knowledge import GameKnowledgeAuthoringService
from app.domain_plugins.game_modeling.models import (
    GameFormulaCandidateCreateRequest,
    GamePatchCandidateCreateRequest,
)
from app.domain_plugins.models import (
    ProjectDomainPluginUpdateRequest,
    WorkspaceTaskPluginBindRequest,
)
from app.domain_plugins.service import DomainPluginService
from app.memory.schemas import (
    ArtifactCreateRequest,
    ArtifactNextVersionRequest,
    ArtifactStatusRequest,
    DecisionCreateRequest,
    DecisionStatusRequest,
    MemoryProposalCreateRequest,
    MemoryProposalReviewRequest,
    ProjectCreateRequest,
    ProjectKnowledgeScopeReplaceRequest,
    ProjectStatusRequest,
    ProjectUpdateRequest,
    RevisionRequest,
    WorkspaceTaskCreateRequest,
    WorkspaceTaskStatusRequest,
    WorkspaceTaskUpdateRequest,
)
from app.memory.service import MemoryService
from app.workspace.service import WorkspaceProjectionService


def build_projects_router(
    memory: MemoryService,
    domain_plugins: DomainPluginService,
    game_knowledge: GameKnowledgeAuthoringService,
    workspace: WorkspaceProjectionService,
) -> APIRouter:
    router = APIRouter(tags=["projects"])

    @router.get("/api/v1/workspace/dashboard")
    def get_workspace_dashboard(
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        return workspace_call(lambda: workspace.dashboard(limit=limit)).model_dump()

    @router.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreateRequest) -> dict[str, Any]:
        return memory_call(lambda: memory.create_project(payload)).model_dump()

    @router.get("/api/projects")
    def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.repository.list_projects(include_archived=include_archived)
            )
        ]

    @router.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return memory_call(lambda: memory.repository.get_project(project_id)).model_dump()

    @router.patch("/api/projects/{project_id}")
    def update_project(project_id: str, payload: ProjectUpdateRequest) -> dict[str, Any]:
        return memory_call(lambda: memory.update_project(project_id, payload)).model_dump()

    @router.patch("/api/projects/{project_id}/status")
    def transition_project(project_id: str, payload: ProjectStatusRequest) -> dict[str, Any]:
        return memory_call(lambda: memory.transition_project(project_id, payload)).model_dump()

    @router.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str, payload: RevisionRequest) -> dict[str, Any]:
        status_payload = ProjectStatusRequest(
            expected_revision=payload.expected_revision, status="archived"
        )
        return memory_call(
            lambda: memory.transition_project(project_id, status_payload)
        ).model_dump()

    @router.get("/api/projects/{project_id}/memory")
    def get_project_memory_snapshot(project_id: str) -> dict[str, Any]:
        return memory_call(lambda: memory.snapshot(project_id)).model_dump()

    @router.get("/api/projects/{project_id}/knowledge-scopes")
    def list_project_knowledge_scopes(project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.repository.list_project_knowledge_scopes(project_id)
            )
        ]

    @router.put("/api/projects/{project_id}/knowledge-scopes")
    def replace_project_knowledge_scopes(
        project_id: str, payload: ProjectKnowledgeScopeReplaceRequest
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.replace_knowledge_scopes(project_id, payload)
            )
        ]

    @router.get("/api/v1/domain-plugins")
    def list_domain_plugins() -> list[dict[str, Any]]:
        return [item.model_dump() for item in domain_plugins.list_available()]

    @router.get("/api/v1/projects/{project_id}/domain-plugins")
    def list_project_domain_plugins(project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in domain_plugin_call(
                lambda: domain_plugins.list_project_bindings(project_id)
            )
        ]

    @router.put("/api/v1/projects/{project_id}/domain-plugins/{plugin_key}")
    def set_project_domain_plugin(
        project_id: str,
        plugin_key: str,
        payload: ProjectDomainPluginUpdateRequest,
    ) -> dict[str, Any]:
        return domain_plugin_call(
            lambda: domain_plugins.set_project_binding(project_id, plugin_key, payload)
        ).model_dump()

    @router.patch("/api/v1/workspace-tasks/{task_id}/domain-plugin")
    def bind_workspace_task_domain_plugin(
        task_id: str, payload: WorkspaceTaskPluginBindRequest
    ) -> dict[str, Any]:
        return domain_plugin_call(
            lambda: domain_plugins.bind_workspace_task(task_id, payload)
        ).model_dump()

    @router.post("/api/v1/domain-plugins/game_modeling/patch-candidates", status_code=201)
    def create_game_patch_candidate(
        payload: GamePatchCandidateCreateRequest,
    ) -> dict[str, Any]:
        return game_knowledge_call(
            lambda: game_knowledge.create_patch_candidate(payload)
        ).model_dump()

    @router.post("/api/v1/domain-plugins/game_modeling/formula-candidates", status_code=201)
    def create_game_formula_candidate(
        payload: GameFormulaCandidateCreateRequest,
    ) -> dict[str, Any]:
        return game_knowledge_call(
            lambda: game_knowledge.create_formula_candidate(payload)
        ).model_dump()

    @router.post("/api/v1/domain-plugins/game_modeling/candidates/{candidate_id}/approve")
    def approve_game_knowledge_candidate(candidate_id: str) -> dict[str, Any]:
        return game_knowledge_call(
            lambda: game_knowledge.approve_candidate(candidate_id)
        ).model_dump()

    @router.post("/api/v1/domain-plugins/game_modeling/candidates/{candidate_id}/reject")
    def reject_game_knowledge_candidate(
        candidate_id: str, payload: GameKnowledgeReviewRequest
    ) -> dict[str, Any]:
        return game_knowledge_call(
            lambda: game_knowledge.reject_candidate(
                candidate_id, review_note=payload.review_note
            )
        ).model_dump()

    @router.post("/api/projects/{project_id}/workspace-tasks", status_code=201)
    def create_workspace_task(
        project_id: str, payload: WorkspaceTaskCreateRequest
    ) -> dict[str, Any]:
        return memory_call(
            lambda: memory.create_workspace_task(project_id, payload)
        ).model_dump()

    @router.get("/api/projects/{project_id}/workspace-tasks")
    def list_workspace_tasks(
        project_id: str, include_closed: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.repository.list_workspace_tasks(
                    project_id, include_closed=include_closed
                )
            )
        ]

    @router.get("/api/workspace-tasks/{task_id}")
    def get_workspace_task(task_id: str) -> dict[str, Any]:
        return memory_call(
            lambda: memory.repository.get_workspace_task(task_id)
        ).model_dump()

    @router.patch("/api/workspace-tasks/{task_id}")
    def update_workspace_task(
        task_id: str, payload: WorkspaceTaskUpdateRequest
    ) -> dict[str, Any]:
        return memory_call(lambda: memory.update_workspace_task(task_id, payload)).model_dump()

    @router.patch("/api/workspace-tasks/{task_id}/status")
    def transition_workspace_task(
        task_id: str, payload: WorkspaceTaskStatusRequest
    ) -> dict[str, Any]:
        return memory_call(
            lambda: memory.transition_workspace_task(task_id, payload)
        ).model_dump()

    @router.post("/api/projects/{project_id}/decisions", status_code=201)
    def create_memory_decision(
        project_id: str, payload: DecisionCreateRequest
    ) -> dict[str, Any]:
        return memory_call(lambda: memory.create_decision(project_id, payload)).model_dump()

    @router.get("/api/projects/{project_id}/decisions")
    def list_memory_decisions(
        project_id: str, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.repository.list_decisions(
                    project_id, include_inactive=include_inactive
                )
            )
        ]

    @router.get("/api/decisions/{decision_id}")
    def get_memory_decision(decision_id: str) -> dict[str, Any]:
        return memory_call(lambda: memory.repository.get_decision(decision_id)).model_dump()

    @router.patch("/api/decisions/{decision_id}/status")
    def transition_memory_decision(
        decision_id: str, payload: DecisionStatusRequest
    ) -> dict[str, Any]:
        return memory_call(
            lambda: memory.transition_decision(decision_id, payload)
        ).model_dump()

    @router.post("/api/projects/{project_id}/artifacts", status_code=201)
    def create_artifact(project_id: str, payload: ArtifactCreateRequest) -> dict[str, Any]:
        return memory_call(lambda: memory.create_artifact(project_id, payload)).model_dump()

    @router.get("/api/projects/{project_id}/artifacts")
    def list_artifacts(
        project_id: str, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.repository.list_artifacts(
                    project_id, include_inactive=include_inactive
                )
            )
        ]

    @router.get("/api/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        return memory_call(lambda: memory.repository.get_artifact(artifact_id)).model_dump()

    @router.get("/api/v1/artifacts/{artifact_id}/content")
    def get_artifact_content(artifact_id: str) -> dict[str, Any]:
        return workspace_call(lambda: workspace.get_artifact_content(artifact_id)).model_dump()

    @router.post("/api/artifacts/{artifact_id}/versions", status_code=201)
    def create_next_artifact_version(
        artifact_id: str, payload: ArtifactNextVersionRequest
    ) -> dict[str, Any]:
        return memory_call(
            lambda: memory.create_next_artifact_version(artifact_id, payload)
        ).model_dump()

    @router.patch("/api/artifacts/{artifact_id}/status")
    def transition_artifact(
        artifact_id: str, payload: ArtifactStatusRequest
    ) -> dict[str, Any]:
        return memory_call(
            lambda: memory.transition_artifact(artifact_id, payload)
        ).model_dump()

    @router.post("/api/projects/{project_id}/memory-proposals", status_code=201)
    def create_memory_proposal(
        project_id: str, payload: MemoryProposalCreateRequest
    ) -> dict[str, Any]:
        return memory_call(lambda: memory.create_proposal(project_id, payload)).model_dump()

    @router.get("/api/projects/{project_id}/memory-proposals")
    def list_memory_proposals(
        project_id: str, include_closed: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in memory_call(
                lambda: memory.repository.list_proposals(
                    project_id, include_closed=include_closed
                )
            )
        ]

    @router.get("/api/memory-proposals/{proposal_id}")
    def get_memory_proposal(proposal_id: str) -> dict[str, Any]:
        return memory_call(lambda: memory.repository.get_proposal(proposal_id)).model_dump()

    @router.post("/api/memory-proposals/{proposal_id}/review")
    def review_memory_proposal(
        proposal_id: str, payload: MemoryProposalReviewRequest
    ) -> dict[str, Any]:
        return memory_call(
            lambda: memory.review_proposal(proposal_id, payload)
        ).model_dump()

    @router.post("/api/memory-proposals/{proposal_id}/commit")
    def commit_memory_proposal(proposal_id: str) -> dict[str, Any]:
        return memory_call(lambda: memory.commit_proposal(proposal_id)).model_dump()

    return router
