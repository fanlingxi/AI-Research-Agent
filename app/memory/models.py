from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ProjectStatus = Literal["active", "paused", "completed", "archived"]
WorkspaceTaskStatus = Literal[
    "backlog", "ready", "in_progress", "blocked", "completed", "cancelled"
]
WorkspaceTaskPriority = Literal["low", "normal", "high", "urgent"]
DecisionStatus = Literal["proposed", "accepted", "superseded", "rejected"]
ArtifactStatus = Literal["draft", "ready", "superseded", "archived"]
MemoryProposalStatus = Literal["proposed", "approved", "rejected", "committed", "cancelled"]
MemoryProposalType = Literal[
    "decision_create",
    "artifact_create",
    "workspace_task_create",
    "project_update",
    "workspace_task_update",
]


class Project(BaseModel):
    id: str
    name: str
    goal: str = ""
    domain: str = ""
    status: ProjectStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str


class WorkspaceTask(BaseModel):
    id: str
    project_id: str
    title: str
    goal: str = ""
    status: WorkspaceTaskStatus
    priority: WorkspaceTaskPriority
    # Routing metadata only. The task owns no plugin implementation and no
    # additional Knowledge/Memory state.
    domain_plugin_key: str = "research"
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str


class MemoryDecision(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    summary: str
    rationale: str = ""
    impact: str = ""
    status: DecisionStatus
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str


class Artifact(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    type: str
    reference: str
    version: int
    status: ArtifactStatus
    supersedes_artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str


class ProjectKnowledgeScope(BaseModel):
    project_id: str
    collection_slug: str
    created_at: str


class ProjectDomainPlugin(BaseModel):
    """A Project-scoped enablement record for one statically installed plugin."""

    project_id: str
    plugin_key: str
    status: Literal["enabled", "disabled"]
    config: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str


class MemoryProposal(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    proposal_type: MemoryProposalType
    payload: dict[str, Any]
    rationale: str = ""
    status: MemoryProposalStatus
    review_note: str | None = None
    committed_record_type: str | None = None
    committed_record_id: str | None = None
    revision: int
    created_at: str
    reviewed_at: str | None = None
    committed_at: str | None = None
    updated_at: str


class ProjectMemorySnapshot(BaseModel):
    project: Project
    workspace_tasks: list[WorkspaceTask] = Field(default_factory=list)
    decisions: list[MemoryDecision] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    knowledge_scopes: list[ProjectKnowledgeScope] = Field(default_factory=list)


class MemoryProposalCommitResult(BaseModel):
    proposal: MemoryProposal
    record: Project | WorkspaceTask | MemoryDecision | Artifact
