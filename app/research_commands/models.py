from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel, model_validator


class ExistingTaskSelection(BaseModel):
    kind: Literal["existing"]
    task_id: str = Field(min_length=1)


class NewTaskSelection(BaseModel):
    kind: Literal["new"]
    title: str | None = Field(default=None, max_length=400)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


TaskSelection = Annotated[
    ExistingTaskSelection | NewTaskSelection,
    Field(discriminator="kind"),
]


class QuickReportCommand(BaseModel):
    mode: Literal["quick_report"] = "quick_report"
    instruction: str = Field(min_length=3, max_length=1000)
    collection_slugs: list[str] = Field(default_factory=list, max_length=20)
    report_depth: Literal["brief", "standard", "deep"] = "standard"
    top_k: int = Field(default=12, ge=1, le=30)


class ProjectRunCommand(BaseModel):
    mode: Literal["project_run"]
    instruction: str = Field(min_length=3, max_length=4000)
    project_id: str = Field(min_length=1)
    task: TaskSelection
    create_memory_proposal: bool = False
    max_steps: int = Field(default=10, ge=3, le=16)
    max_tool_calls: int = Field(default=3, ge=0, le=8)
    token_budget: int = Field(default=6000, ge=256, le=16000)


CommandPayload = Annotated[
    QuickReportCommand | ProjectRunCommand,
    Field(discriminator="mode"),
]


class ResearchCommandSubmission(RootModel[CommandPayload]):
    @model_validator(mode="before")
    @classmethod
    def default_to_quick_report(cls, value):
        if isinstance(value, dict) and "mode" not in value:
            return {**value, "mode": "quick_report"}
        return value


class ResearchCommand(BaseModel):
    id: str
    mode: Literal["quick_report", "project_run"]
    status: Literal["accepted", "preparing", "target_created", "queued", "completed", "failed"]
    instruction: str
    orchestration_stage: str
    project_id: str | None = None
    task_id: str | None = None
    snapshot_id: str | None = None
    target_resource_type: str | None = None
    target_resource_id: str | None = None
    target_route: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class ResearchCommandConflictError(ValueError):
    """An Idempotency-Key was reused with a different normalized request."""
