from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MemoryRecord(BaseModel):
    """One durable memory item from a completed research run."""

    id: str
    query: str
    summary: str
    created_at: str
    quality_score: float = 0.0
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySnapshot(BaseModel):
    """Retrieved memory context for a new research run."""

    records: list[MemoryRecord] = Field(default_factory=list)
    summary: str = ""
