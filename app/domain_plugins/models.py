"""HTTP- and service-facing models for Project/Task plugin routing."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.memory.models import Project, ProjectDomainPlugin, WorkspaceTask


class ProjectDomainPluginUpdateRequest(BaseModel):
    expected_project_revision: int = Field(ge=1)
    status: Literal["enabled", "disabled"] = "enabled"
    config: dict[str, Any] = Field(default_factory=dict)


class WorkspaceTaskPluginBindRequest(BaseModel):
    expected_revision: int = Field(ge=1)
    plugin_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class ProjectDomainPluginUpdateResult(BaseModel):
    project: Project
    binding: ProjectDomainPlugin


class WorkspaceTaskPluginBindResult(BaseModel):
    task: WorkspaceTask
