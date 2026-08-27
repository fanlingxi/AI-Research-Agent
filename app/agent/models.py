"""Serializable business contracts for Phase 3A Agent Runs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domain_plugins.contracts import PluginPin
from app.memory.models import Artifact, MemoryProposal

AgentRunStatus = Literal[
    "created",
    "queued",
    "preparing",
    "running",
    "validating",
    "needs_review",
    "completed",
    "failed",
    "cancelled",
    "stale_context",
]
AgentToolCallStatus = Literal["completed", "failed"]
ToolPermission = Literal["context_read", "runtime_read", "deterministic_compute"]
# Workflow keys are validated by the pinned Domain Plugin, not by a global
# Literal. This lets new first-party plugins extend Runtime without editing
# Agent Core while retaining the current Research aliases.
AgentWorkflow = str


class AgentRunOptions(BaseModel):
    """Immutable execution choices captured with one AgentRun."""

    workflow: AgentWorkflow = Field(default="research", min_length=2, max_length=64)
    create_memory_proposal: bool = False


class AgentRunCreateRequest(BaseModel):
    """Bounded request resolved against the WorkspaceTask's Domain Plugin."""

    context_snapshot_id: str | None = Field(default=None, min_length=1)
    # Omitted requests retain the established Research default. A different
    # Plugin may supply its own default workflow once it is bound to the task.
    workflow: AgentWorkflow | None = Field(default=None, min_length=2, max_length=64)
    create_memory_proposal: bool = False
    # The deterministic foundation graph has three nodes.  A smaller bound
    # cannot represent a valid Phase 3A execution.
    max_steps: int = Field(default=10, ge=3, le=16)
    max_tool_calls: int = Field(default=1, ge=0, le=8)
    token_budget: int = Field(default=6000, ge=256, le=16000)

    @model_validator(mode="after")
    def has_a_bounded_execution_path(self):
        # Domain-specific lower bounds are validated only after the Task's
        # authoritative plugin binding has been loaded.
        return self


class AgentRunReviewRequest(BaseModel):
    """The two safe exits from a citation-validation review stop."""

    action: Literal["rerun", "close"]


class AgentRun(BaseModel):
    id: str
    project_id: str
    task_id: str
    context_snapshot_id: str
    context_sha256: str
    workflow_name: str
    workflow_version: str
    plugin: PluginPin
    options: AgentRunOptions = Field(default_factory=AgentRunOptions)
    status: AgentRunStatus
    model_provider: str
    model_name: str
    max_steps: int
    max_tool_calls: int
    token_budget: int
    tool_call_count: int
    repair_count: int
    current_node: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    created_at: str
    updated_at: str
    revision: int


class AgentRunEvent(BaseModel):
    id: str
    run_id: str
    sequence: int
    event_type: str
    node_name: str | None = None
    status: str
    input_summary: dict[str, Any] = Field(default_factory=dict)
    output_summary: dict[str, Any] = Field(default_factory=dict)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: float | None = None
    error: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AgentToolCall(BaseModel):
    id: str
    run_id: str
    event_id: str
    sequence: int
    tool_name: str
    permission: ToolPermission
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    result_hash: str
    status: AgentToolCallStatus
    idempotency_key: str
    started_at: str
    completed_at: str | None = None
    error: dict[str, Any] = Field(default_factory=dict)


class AgentRunOutput(BaseModel):
    id: str
    run_id: str
    output_type: str
    structured: dict[str, Any]
    rendered_text: str
    output_sha256: str
    validation: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class DomainFinalization(BaseModel):
    """The indivisible business records created by one validated plugin run."""

    output: AgentRunOutput
    artifact: Artifact
    memory_proposal: MemoryProposal | None = None


class ResearchFinalization(DomainFinalization):
    """Compatibility alias for callers of the frozen Research service API."""
