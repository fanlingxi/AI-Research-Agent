"""Bounded response contracts consumed by the future Workspace UI."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.models import AgentRun, AgentRunEvent, AgentToolCall
from app.context.models import ContextSnapshotItem
from app.memory.models import Artifact, MemoryProposal, Project, WorkspaceTask


class ContextProjectionRequest(BaseModel):
    """UI-owned context controls; collection authority stays server-derived."""

    max_tokens: int = Field(default=6000, ge=256, le=16000)


class ContextSnapshotSummary(BaseModel):
    """Small snapshot envelope, deliberately excluding the runtime payload."""

    id: str | None = None
    persisted: bool
    project_id: str
    task_id: str
    project_revision: int
    task_revision: int
    builder_version: str
    package_schema_version: str
    package_sha256: str
    token_budget: int
    used_tokens: int
    collection_scopes: list[str] = Field(default_factory=list)
    item_counts: dict[str, int] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ContextPreviewProjection(BaseModel):
    """A non-persisted, summary-only Context Builder result."""

    snapshot: ContextSnapshotSummary


class ContextSnapshotItemPage(BaseModel):
    snapshot_id: str
    items: list[ContextSnapshotItem] = Field(default_factory=list)
    next_offset: int | None = None


class EvidenceReferenceProjection(BaseModel):
    """Locatable evidence selected by one immutable ContextSnapshot."""

    snapshot_id: str
    claim: dict[str, Any]
    evidence: dict[str, Any]
    chunk: dict[str, Any]
    document: dict[str, Any]
    source: dict[str, Any]


class AgentRunTraceProjection(BaseModel):
    """A paged, safe audit trace without checkpoint or prompt contents."""

    run: AgentRun
    events: list[AgentRunEvent] = Field(default_factory=list)
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    next_event_sequence: int | None = None
    token_usage: dict[str, int] = Field(default_factory=dict)
    total_latency_ms: float = 0.0


class ArtifactContentProjection(BaseModel):
    """Markdown rendered from an Agent output reference, never a local file."""

    artifact_id: str
    reference: str
    content_type: Literal["text/markdown"] = "text/markdown"
    rendered_markdown: str
    output_id: str
    output_sha256: str
    run_id: str
    context_snapshot_id: str
    context_sha256: str
    validation: dict[str, Any] = Field(default_factory=dict)


class WorkspaceDashboardProjection(BaseModel):
    """Bounded dashboard summary for the local Project Workspace."""

    active_projects: list[Project] = Field(default_factory=list)
    recent_workspace_tasks: list[WorkspaceTask] = Field(default_factory=list)
    recent_agent_runs: list[AgentRun] = Field(default_factory=list)
    pending_memory_proposals: list[MemoryProposal] = Field(default_factory=list)
    recent_artifacts: list[Artifact] = Field(default_factory=list)
