from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RuntimeServiceState(BaseModel):
    available: bool
    configured: bool = True
    detail: str
    endpoint: str | None = None


class RuntimeExecutorState(BaseModel):
    id: str
    role: str
    version: str
    online: bool
    started_at: str
    last_heartbeat_at: str
    current_job_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectionBacklog(BaseModel):
    queued: int = 0
    running: int = 0
    failed: int = 0
    completed: int = 0
    oldest_queued_at: str | None = None


class RuntimeOverview(BaseModel):
    status: str
    generated_at: str
    services: dict[str, RuntimeServiceState]
    executors: list[RuntimeExecutorState]
    work_counts: dict[str, dict[str, int]]
    projection_backlog: ProjectionBacklog


class RuntimeWorkItem(BaseModel):
    id: str
    kind: str
    resource_id: str
    title: str
    detail_route: str
    business_status: str
    job_status: str
    current_stage: str | None = None
    attempt: int = 0
    priority: int = 0
    queue_position: int | None = None
    lease_until: str | None = None
    executor: str | None = None
    created_at: str
    updated_at: str
    last_error: str | None = None
    can_retry: bool = False
    can_cancel: bool = False


class RuntimeWorkPage(BaseModel):
    items: list[RuntimeWorkItem]
    next_cursor: str | None = None
