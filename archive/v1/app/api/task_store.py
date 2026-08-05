from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

from app.graph.state import ResearchState


@dataclass
class ResearchTask:
    """In-process task record used by the local FastAPI deployment."""

    id: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(tz=UTC).isoformat())
    error: str | None = None
    result: ResearchState | None = None

    def snapshot(self) -> dict:
        evaluation = self.result.get("evaluation_result") if self.result else None
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "run_id": self.result.get("run_id") if self.result else None,
            "evaluation": _dump(evaluation),
            "obsidian_export": self.result.get("obsidian_export_result") if self.result else None,
        }


class ResearchTaskStore:
    """Thread-safe ephemeral task registry for a single FastAPI process."""

    def __init__(self) -> None:
        self._tasks: dict[str, ResearchTask] = {}
        self._lock = Lock()

    def create(self) -> ResearchTask:
        task = ResearchTask(id=f"task-{uuid4().hex}")
        with self._lock:
            self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> ResearchTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def start(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = "running"
            task.updated_at = datetime.now(tz=UTC).isoformat()

    def complete(self, task_id: str, result: ResearchState) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = "completed"
            task.result = result
            task.updated_at = datetime.now(tz=UTC).isoformat()

    def fail(self, task_id: str, error: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = "failed"
            task.error = error
            task.updated_at = datetime.now(tz=UTC).isoformat()


def _dump(value):
    return value.model_dump() if hasattr(value, "model_dump") else value
