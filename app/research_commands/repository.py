from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.execution import ExecutionFence
from app.knowledge.repository import KnowledgeRepository
from app.research_commands.models import ResearchCommand, ResearchCommandConflictError


class ResearchCommandRepository:
    def __init__(self, knowledge_repository: KnowledgeRepository) -> None:
        self.knowledge_repository = knowledge_repository

    def create_or_get(
        self,
        *,
        idempotency_key: str,
        normalized_request: dict[str, Any],
    ) -> tuple[ResearchCommand, bool]:
        serialized = _dump(normalized_request)
        request_sha256 = hashlib.sha256(serialized.encode()).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM research_commands WHERE idempotency_key_hash = ?", (key_hash,)
            ).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != request_sha256:
                    raise ResearchCommandConflictError(
                        "Idempotency-Key is already bound to a different research command."
                    )
                return self._from_row(existing), False
            command_id = f"research-command-{uuid4().hex}"
            now = _now()
            connection.execute(
                """
                INSERT INTO research_commands (
                    id, idempotency_key_hash, request_sha256, mode, status,
                    instruction, request_json, orchestration_stage,
                    project_id, task_id, snapshot_id, target_resource_type,
                    target_resource_id, target_route, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'accepted', ?, ?, 'accepted', ?, NULL, NULL,
                          NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    command_id,
                    key_hash,
                    request_sha256,
                    normalized_request["mode"],
                    str(normalized_request["instruction"]).strip(),
                    serialized,
                    normalized_request.get("project_id"),
                    now,
                    now,
                ),
            )
            self.knowledge_repository._enqueue_job_tx(
                connection,
                kind="research_command",
                resource_id=command_id,
                payload={"mode": normalized_request["mode"]},
                priority=120,
            )
            row = connection.execute(
                "SELECT * FROM research_commands WHERE id = ?", (command_id,)
            ).fetchone()
        return self._from_row(row), True

    def get(self, command_id: str) -> ResearchCommand:
        with self.knowledge_repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_commands WHERE id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return self._from_row(row)

    def request_payload(self, command_id: str) -> dict[str, Any]:
        with self.knowledge_repository._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM research_commands WHERE id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return json.loads(row["request_json"])

    def find_id_by_target(self, resource_type: str, resource_id: str) -> str | None:
        with self.knowledge_repository._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM research_commands
                WHERE target_resource_type = ? AND target_resource_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (resource_type, resource_id),
            ).fetchone()
        return None if row is None else str(row["id"])

    def mark_preparing(self, command_id: str, fence: ExecutionFence) -> ResearchCommand:
        return self.update_progress(
            command_id,
            fence=fence,
            status="preparing",
            orchestration_stage="preparing",
        )

    def update_progress(
        self,
        command_id: str,
        *,
        fence: ExecutionFence,
        status: str,
        orchestration_stage: str,
        task_id: str | None = None,
        snapshot_id: str | None = None,
        target_resource_type: str | None = None,
        target_resource_id: str | None = None,
        target_route: str | None = None,
    ) -> ResearchCommand:
        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.knowledge_repository.assert_job_ownership_tx(
                connection,
                job_id=fence.job_id,
                kind="research_command",
                resource_id=command_id,
                expected_attempt=fence.attempt,
                expected_owner=fence.owner_id,
            )
            connection.execute(
                """
                UPDATE research_commands
                SET status = ?, orchestration_stage = ?,
                    task_id = COALESCE(?, task_id),
                    snapshot_id = COALESCE(?, snapshot_id),
                    target_resource_type = COALESCE(?, target_resource_type),
                    target_resource_id = COALESCE(?, target_resource_id),
                    target_route = COALESCE(?, target_route),
                    error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    orchestration_stage,
                    task_id,
                    snapshot_id,
                    target_resource_type,
                    target_resource_id,
                    target_route,
                    _now(),
                    command_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM research_commands WHERE id = ?", (command_id,)
            ).fetchone()
        return self._from_row(row)

    def mark_failed(
        self,
        command_id: str,
        *,
        fence: ExecutionFence,
        error: str,
    ) -> ResearchCommand:
        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.knowledge_repository.assert_job_ownership_tx(
                connection,
                job_id=fence.job_id,
                kind="research_command",
                resource_id=command_id,
                expected_attempt=fence.attempt,
                expected_owner=fence.owner_id,
            )
            connection.execute(
                """
                UPDATE research_commands
                SET status = 'failed', error = ?, updated_at = ? WHERE id = ?
                """,
                (error[:4000], _now(), command_id),
            )
            row = connection.execute(
                "SELECT * FROM research_commands WHERE id = ?", (command_id,)
            ).fetchone()
        return self._from_row(row)

    def reflect_target(
        self,
        command_id: str,
        *,
        status: str,
        orchestration_stage: str,
        error: str | None = None,
    ) -> ResearchCommand:
        with self.knowledge_repository._connect() as connection:
            connection.execute(
                """
                UPDATE research_commands
                SET status = ?, orchestration_stage = ?, error = ?, updated_at = ?
                WHERE id = ?
                  AND (
                    status <> ? OR orchestration_stage <> ?
                    OR COALESCE(error, '') <> COALESCE(?, '')
                  )
                """,
                (
                    status,
                    orchestration_stage,
                    error,
                    _now(),
                    command_id,
                    status,
                    orchestration_stage,
                    error,
                ),
            )
        return self.get(command_id)

    def retry_orchestration(self, command_id: str) -> ResearchCommand:
        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            command = connection.execute(
                "SELECT * FROM research_commands WHERE id = ?", (command_id,)
            ).fetchone()
            if command is None:
                raise KeyError(command_id)
            job = connection.execute(
                """
                SELECT * FROM knowledge_jobs
                WHERE kind = 'research_command' AND resource_id = ?
                """,
                (command_id,),
            ).fetchone()
            if command["status"] != "failed" or job is None or job["status"] != "failed":
                raise ValueError("Only a failed orchestration can be retried here.")
            connection.execute(
                """
                UPDATE research_commands
                SET status = 'accepted', orchestration_stage = 'retry_accepted',
                    error = NULL, updated_at = ? WHERE id = ?
                """,
                (_now(), command_id),
            )
            self.knowledge_repository._enqueue_job_tx(
                connection,
                kind="research_command",
                resource_id=command_id,
                payload={"mode": command["mode"]},
                priority=120,
                force_requeue=True,
            )
        return self.get(command_id)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ResearchCommand:
        return ResearchCommand(
            id=row["id"],
            mode=row["mode"],
            status=row["status"],
            instruction=row["instruction"],
            orchestration_stage=row["orchestration_stage"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            snapshot_id=row["snapshot_id"],
            target_resource_type=row["target_resource_type"],
            target_resource_id=row["target_resource_id"],
            target_route=row["target_route"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
