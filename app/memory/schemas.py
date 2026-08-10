from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.memory.models import (
    ArtifactStatus,
    DecisionStatus,
    ProjectStatus,
    WorkspaceTaskPriority,
    WorkspaceTaskStatus,
)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=240)
    goal: str = Field(default="", max_length=4000)
    domain: str = Field(default="", max_length=240)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=2, max_length=240)
    goal: str | None = Field(default=None, max_length=4000)
    domain: str | None = Field(default=None, max_length=240)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def includes_change(self):
        if all(value is None for value in (self.name, self.goal, self.domain, self.metadata)):
            raise ValueError("at least one Project field must be supplied")
        return self


class ProjectStatusRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    status: ProjectStatus


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


class WorkspaceTaskCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=400)
    goal: str = Field(default="", max_length=4000)
    priority: WorkspaceTaskPriority = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceTaskUpdateRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=2, max_length=400)
    goal: str | None = Field(default=None, max_length=4000)
    priority: WorkspaceTaskPriority | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def includes_change(self):
        if all(value is None for value in (self.title, self.goal, self.priority, self.metadata)):
            raise ValueError("at least one WorkspaceTask field must be supplied")
        return self


class WorkspaceTaskStatusRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    status: WorkspaceTaskStatus


class DecisionCreateRequest(BaseModel):
    task_id: str | None = None
    summary: str = Field(min_length=2, max_length=2000)
    rationale: str = Field(default="", max_length=5000)
    impact: str = Field(default="", max_length=3000)
    # Direct human entry may begin a decision, but terminal states must result
    # from an explicit transition after creation.
    status: Literal["proposed", "accepted"] = "accepted"
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionStatusRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    status: DecisionStatus


class ArtifactCreateRequest(BaseModel):
    task_id: str | None = None
    type: str = Field(min_length=2, max_length=120)
    reference: str = Field(min_length=1, max_length=4000)
    # Direct human entry may register a draft or ready artifact only.  Terminal
    # states are produced by the Artifact state machine.
    status: Literal["draft", "ready"] = "ready"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactNextVersionRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    reference: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, Any] | None = None


class ArtifactStatusRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    status: ArtifactStatus


class ProjectKnowledgeScopeReplaceRequest(BaseModel):
    expected_project_revision: int = Field(ge=1)
    collection_slugs: list[str] = Field(default_factory=list, max_length=100)


class DecisionCreateProposalPayload(BaseModel):
    proposal_type: Literal["decision_create"]
    summary: str = Field(min_length=2, max_length=2000)
    rationale: str = Field(default="", max_length=5000)
    impact: str = Field(default="", max_length=3000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreateProposalPayload(BaseModel):
    proposal_type: Literal["artifact_create"]
    type: str = Field(min_length=2, max_length=120)
    reference: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceTaskCreateProposalPayload(BaseModel):
    proposal_type: Literal["workspace_task_create"]
    title: str = Field(min_length=2, max_length=400)
    goal: str = Field(default="", max_length=4000)
    priority: WorkspaceTaskPriority = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateProposalPayload(BaseModel):
    proposal_type: Literal["project_update"]
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=2, max_length=240)
    goal: str | None = Field(default=None, max_length=4000)
    domain: str | None = Field(default=None, max_length=240)
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def includes_change(self):
        if all(value is None for value in (self.name, self.goal, self.domain, self.metadata)):
            raise ValueError("a project update proposal must include a change")
        return self


class WorkspaceTaskUpdateProposalPayload(BaseModel):
    proposal_type: Literal["workspace_task_update"]
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=2, max_length=400)
    goal: str | None = Field(default=None, max_length=4000)
    priority: WorkspaceTaskPriority | None = None
    status: WorkspaceTaskStatus | None = None
    metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def includes_change(self):
        if all(
            value is None
            for value in (self.title, self.goal, self.priority, self.status, self.metadata)
        ):
            raise ValueError("a workspace task update proposal must include a change")
        return self


MemoryProposalPayload = Annotated[
    DecisionCreateProposalPayload
    | ArtifactCreateProposalPayload
    | WorkspaceTaskCreateProposalPayload
    | ProjectUpdateProposalPayload
    | WorkspaceTaskUpdateProposalPayload,
    Field(discriminator="proposal_type"),
]


class MemoryProposalCreateRequest(BaseModel):
    task_id: str | None = None
    rationale: str = Field(default="", max_length=5000)
    payload: MemoryProposalPayload


class MemoryProposalReviewRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    status: Literal["approved", "rejected"]
    review_note: str | None = Field(default=None, max_length=3000)


def validate_memory_proposal_payload(value: dict[str, Any]) -> MemoryProposalPayload:
    """Validate persisted proposal JSON before any commit can mutate Memory."""

    proposal_type = value.get("proposal_type")
    by_type = {
        "decision_create": DecisionCreateProposalPayload,
        "artifact_create": ArtifactCreateProposalPayload,
        "workspace_task_create": WorkspaceTaskCreateProposalPayload,
        "project_update": ProjectUpdateProposalPayload,
        "workspace_task_update": WorkspaceTaskUpdateProposalPayload,
    }
    model = by_type.get(proposal_type)
    if model is None:
        raise ValueError("unsupported MemoryProposal proposal_type")
    return model.model_validate(value)
