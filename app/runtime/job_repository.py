from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from app.knowledge.schemas import ExecutorHeartbeat, KnowledgeJob
from app.persistence.sqlite import SQLiteDatabase


class JobRepository:
    """Durable queue and executor heartbeats on the application's shared SQLite database.

    Transaction methods use the caller's connection so business state and queue
    changes commit together. Schema setup remains owned by KnowledgeRepository.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def enqueue_job(
        self,
        *,
        kind: Literal["ingestion", "report", "agent_run", "research_command"],
        resource_id: str,
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        force_requeue: bool = False,
    ) -> KnowledgeJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job_id = self.enqueue_job_tx(
                connection,
                kind=kind,
                resource_id=resource_id,
                payload=payload or {},
                priority=priority,
                force_requeue=force_requeue,
            )
        return self.get_job(job_id)

    def enqueue_job_tx(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        resource_id: str,
        payload: dict[str, Any],
        priority: int = 0,
        force_requeue: bool = False,
    ) -> str:
        existing = connection.execute(
            "SELECT id, status FROM knowledge_jobs WHERE kind = ? AND resource_id = ?",
            (kind, resource_id),
        ).fetchone()
        if existing:
            if force_requeue or existing["status"] == "failed":
                connection.execute(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'queued', payload_json = ?, priority = ?,
                        lease_until = NULL, lease_owner = NULL,
                        last_error = NULL, updated_at = ? WHERE id = ?
                    """,
                    (_dump(payload), priority, _now(), existing["id"]),
                )
            return str(existing["id"])
        job_id = f"job-{uuid4().hex}"
        now = _now()
        connection.execute(
            """
            INSERT INTO knowledge_jobs (
                id, kind, resource_id, status, payload_json, attempts,
                priority, lease_until, lease_owner, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, 0, ?, NULL, NULL, NULL, ?, ?)
            """,
            (job_id, kind, resource_id, _dump(payload), priority, now, now),
        )
        return job_id

    def get_job(self, job_id: str) -> KnowledgeJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self.job_from_row(row)

    def get_resource_job(
        self,
        kind: Literal["ingestion", "report", "agent_run", "research_command"],
        resource_id: str,
    ) -> KnowledgeJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE kind = ? AND resource_id = ?",
                (kind, resource_id),
            ).fetchone()
        if row is None:
            raise KeyError(resource_id)
        return self.job_from_row(row)

    def claim_job(
        self,
        lease_seconds: int = 120,
        *,
        owner_id: str | None = None,
    ) -> KnowledgeJob | None:
        now = datetime.now(tz=UTC)
        owner = owner_id or f"executor-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'queued', lease_until = NULL, lease_owner = NULL, updated_at = ?
                WHERE status = 'running' AND lease_until < ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            row = connection.execute(
                """
                SELECT * FROM knowledge_jobs WHERE status = 'queued'
                ORDER BY priority DESC, created_at, id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_until = ?, lease_owner = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lease_until, owner, now.isoformat(), row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        return self.job_from_row(claimed)

    def claim_resource_job(
        self,
        kind: Literal["ingestion", "report", "agent_run", "research_command"],
        resource_id: str,
        lease_seconds: int = 120,
        *,
        owner_id: str | None = None,
    ) -> KnowledgeJob | None:
        """Claim one explicitly selected resource without consuming another queued job."""
        now = datetime.now(tz=UTC)
        now_text = now.isoformat()
        owner = owner_id or f"executor-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE kind = ? AND resource_id = ?",
                (kind, resource_id),
            ).fetchone()
            if row is None:
                raise KeyError(resource_id)
            if row["status"] == "running" and (
                row["lease_until"] is None or str(row["lease_until"]) < now_text
            ):
                connection.execute(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'queued', lease_until = NULL,
                        lease_owner = NULL, updated_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (now_text, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM knowledge_jobs WHERE id = ?", (row["id"],)
                ).fetchone()
            if row["status"] != "queued":
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            updated = connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_until = ?, lease_owner = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lease_until, owner, now_text, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        return self.job_from_row(claimed)

    def complete_job(
        self,
        job_id: str,
        *,
        expected_attempt: int | None = None,
        expected_owner: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            if expected_attempt is None:
                updated = connection.execute(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'completed', lease_until = NULL, lease_owner = NULL,
                        last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (_now(), job_id),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'completed', lease_until = NULL, lease_owner = NULL,
                        last_error = NULL, updated_at = ?
                    WHERE id = ? AND status = 'running' AND attempts = ?
                      AND (? IS NULL OR lease_owner = ?)
                    """,
                    (_now(), job_id, expected_attempt, expected_owner, expected_owner),
                )
        return updated.rowcount == 1

    def complete_resource_job(self, kind: str, resource_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'completed', lease_until = NULL, lease_owner = NULL,
                    last_error = NULL, updated_at = ?
                WHERE kind = ? AND resource_id = ?
                """,
                (_now(), kind, resource_id),
            )

    def renew_job_lease(
        self,
        job_id: str,
        *,
        expected_attempt: int,
        lease_seconds: int,
        expected_owner: str | None = None,
    ) -> bool:
        now = datetime.now(tz=UTC)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE knowledge_jobs SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (
                    lease_until,
                    now.isoformat(),
                    job_id,
                    expected_attempt,
                    expected_owner,
                    expected_owner,
                ),
            )
        return updated.rowcount == 1

    def fail_job(
        self,
        job_id: str,
        error: str,
        *,
        expected_attempt: int | None = None,
        expected_owner: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            if expected_attempt is None:
                updated = connection.execute(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'failed', lease_until = NULL, lease_owner = NULL,
                        last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (error[:4000], _now(), job_id),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE knowledge_jobs
                    SET status = 'failed', lease_until = NULL, lease_owner = NULL,
                        last_error = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND attempts = ?
                      AND (? IS NULL OR lease_owner = ?)
                    """,
                    (
                        error[:4000],
                        _now(),
                        job_id,
                        expected_attempt,
                        expected_owner,
                        expected_owner,
                    ),
                )
        return updated.rowcount == 1

    def assert_job_ownership(
        self,
        *,
        job_id: str,
        kind: str,
        resource_id: str,
        expected_attempt: int,
        expected_owner: str,
    ) -> None:
        with self._connect() as connection:
            self.assert_job_ownership_tx(
                connection,
                job_id=job_id,
                kind=kind,
                resource_id=resource_id,
                expected_attempt=expected_attempt,
                expected_owner=expected_owner,
            )

    @staticmethod
    def assert_job_ownership_tx(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        kind: str,
        resource_id: str,
        expected_attempt: int,
        expected_owner: str,
    ) -> None:
        owned = connection.execute(
            """
            SELECT 1 FROM knowledge_jobs
            WHERE id = ? AND kind = ? AND resource_id = ?
              AND status = 'running' AND attempts = ? AND lease_owner = ?
            """,
            (job_id, kind, resource_id, expected_attempt, expected_owner),
        ).fetchone()
        if owned is None:
            raise RuntimeError(
                f"{kind} {resource_id} is no longer owned by "
                f"{expected_owner} attempt {expected_attempt}."
            )

    def upsert_executor_heartbeat(
        self,
        executor_id: str,
        *,
        role: str,
        version: str,
        started_at: str,
        current_job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutorHeartbeat:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO executor_heartbeats (
                    id, role, version, started_at, last_heartbeat_at,
                    current_job_id, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    role = excluded.role,
                    version = excluded.version,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    current_job_id = excluded.current_job_id,
                    metadata_json = excluded.metadata_json
                """,
                (
                    executor_id,
                    role,
                    version,
                    started_at,
                    now,
                    current_job_id,
                    _dump(metadata or {}),
                ),
            )
            row = connection.execute(
                "SELECT * FROM executor_heartbeats WHERE id = ?", (executor_id,)
            ).fetchone()
        return self._executor_from_row(row)

    def list_executor_heartbeats(self, role: str | None = None) -> list[ExecutorHeartbeat]:
        with self._connect() as connection:
            if role is None:
                rows = connection.execute(
                    "SELECT * FROM executor_heartbeats ORDER BY last_heartbeat_at DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM executor_heartbeats
                    WHERE role = ? ORDER BY last_heartbeat_at DESC
                    """,
                    (role,),
                ).fetchall()
        return [self._executor_from_row(row) for row in rows]

    def job_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM knowledge_jobs GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def job_from_row(self, row: sqlite3.Row) -> KnowledgeJob:
        return KnowledgeJob(
            id=row["id"],
            kind=row["kind"],
            resource_id=row["resource_id"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            attempts=row["attempts"],
            priority=row["priority"],
            lease_until=row["lease_until"],
            lease_owner=row["lease_owner"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _executor_from_row(row: sqlite3.Row) -> ExecutorHeartbeat:
        return ExecutorHeartbeat(
            id=row["id"],
            role=row["role"],
            version=row["version"],
            started_at=row["started_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            current_job_id=row["current_job_id"],
            metadata=json.loads(row["metadata_json"] or "{}"),
        )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
