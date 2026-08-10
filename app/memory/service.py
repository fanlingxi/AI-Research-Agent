from __future__ import annotations

from app.memory.models import ProjectMemorySnapshot
from app.memory.repository import MemoryRepository
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
    WorkspaceTaskCreateRequest,
    WorkspaceTaskStatusRequest,
    WorkspaceTaskUpdateRequest,
)


class MemoryService:
    """Application service for reviewed, long-lived project memory.

    This service has no Agent, projection, retrieval, or file-system behaviour.
    MemoryProposal is the only proposal/review/commit path exposed for future
    automated writers, while direct methods remain explicit user operations.
    """

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def create_project(self, payload: ProjectCreateRequest):
        return self.repository.create_project(**payload.model_dump())

    def update_project(self, project_id: str, payload: ProjectUpdateRequest):
        return self.repository.update_project(project_id, **payload.model_dump())

    def transition_project(self, project_id: str, payload: ProjectStatusRequest):
        return self.repository.transition_project(project_id, **payload.model_dump())

    def create_workspace_task(self, project_id: str, payload: WorkspaceTaskCreateRequest):
        return self.repository.create_workspace_task(project_id=project_id, **payload.model_dump())

    def update_workspace_task(self, task_id: str, payload: WorkspaceTaskUpdateRequest):
        return self.repository.update_workspace_task(task_id, **payload.model_dump())

    def transition_workspace_task(self, task_id: str, payload: WorkspaceTaskStatusRequest):
        return self.repository.transition_workspace_task(task_id, **payload.model_dump())

    def create_decision(self, project_id: str, payload: DecisionCreateRequest):
        return self.repository.create_decision(project_id=project_id, **payload.model_dump())

    def transition_decision(self, decision_id: str, payload: DecisionStatusRequest):
        return self.repository.transition_decision(decision_id, **payload.model_dump())

    def create_artifact(self, project_id: str, payload: ArtifactCreateRequest):
        values = payload.model_dump()
        return self.repository.create_artifact(
            project_id=project_id,
            task_id=values["task_id"],
            artifact_type=values["type"],
            reference=values["reference"],
            status=values["status"],
            metadata=values["metadata"],
        )

    def create_next_artifact_version(
        self, artifact_id: str, payload: ArtifactNextVersionRequest
    ):
        return self.repository.create_next_artifact_version(artifact_id, **payload.model_dump())

    def transition_artifact(self, artifact_id: str, payload: ArtifactStatusRequest):
        return self.repository.transition_artifact(artifact_id, **payload.model_dump())

    def replace_knowledge_scopes(
        self, project_id: str, payload: ProjectKnowledgeScopeReplaceRequest
    ):
        return self.repository.replace_project_knowledge_scopes(
            project_id,
            payload.collection_slugs,
            expected_project_revision=payload.expected_project_revision,
        )

    def create_proposal(self, project_id: str, payload: MemoryProposalCreateRequest):
        return self.repository.create_proposal(
            project_id=project_id,
            task_id=payload.task_id,
            payload=payload.payload.model_dump(),
            rationale=payload.rationale,
        )

    def review_proposal(self, proposal_id: str, payload: MemoryProposalReviewRequest):
        return self.repository.review_proposal(proposal_id, **payload.model_dump())

    def commit_proposal(self, proposal_id: str):
        return self.repository.commit_proposal(proposal_id)

    def snapshot(self, project_id: str) -> ProjectMemorySnapshot:
        return self.repository.snapshot(project_id)
