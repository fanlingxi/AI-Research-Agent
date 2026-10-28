from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from app.config.settings import get_settings
from app.error_redaction import redact_error
from app.knowledge.schemas import ExecutorHeartbeat, KnowledgeJob
from app.persistence.sqlite import SQLiteDatabase

CRASH_RECOVERY_ERROR_CODE = "crash_recovery_exhausted"
CRASH_RECOVERY_ERROR_MESSAGE = "多次异常中断，已停止自动重试，请检查资料后手动重试"


class JobRepository:
    """Durable queue and executor heartbeats on the application's shared SQLite database.

    Transaction methods use the caller's connection so business state and queue
    changes commit together. Schema setup remains owned by KnowledgeRepository.
    """

    def __init__(
        self, database: SQLiteDatabase, *, max_crash_recoveries: int | None = None
    ) -> None:
        self.database = database
        self.max_crash_recoveries = (
            get_settings().knowledge_job_max_crash_recoveries
            if max_crash_recoveries is None else max_crash_recoveries
        )
        if self.max_crash_recoveries < 1:
            raise ValueError("max_crash_recoveries must be at least 1")

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
                        last_error = NULL, crash_recoveries = 0, updated_at = ? WHERE id = ?
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
            self.recover_expired_jobs_tx(connection, now.isoformat())
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
                self.recover_expired_jobs_tx(connection, now_text, job_id=row["id"])
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

    def recover_expired_jobs_tx(
        self, connection: sqlite3.Connection, now: str, *, job_id: str | None = None
    ) -> None:
        """Recover expired leases once, atomically with any business quarantine.

        Callers hold BEGIN IMMEDIATE. Attempts remain monotonic fencing tokens;
        only abnormal lease recovery consumes this separately resettable budget.
        """
        rows = connection.execute(
            """
            SELECT * FROM knowledge_jobs
            WHERE status = 'running' AND (lease_until IS NULL OR lease_until < ?)
              AND (? IS NULL OR id = ?)
            """,
            (now, job_id, job_id),
        ).fetchall()
        for row in rows:
            run = None
            if row["kind"] == "agent_run":
                run = connection.execute(
                    "SELECT status, error_message FROM agent_runs WHERE id = ?",
                    (row["resource_id"],),
                ).fetchone()
                if run is not None and run["status"] in {
                    "completed", "cancelled", "needs_review", "stale_context", "failed",
                }:
                    # AgentRun commits before the Worker acknowledges the job.
                    # A crash in that gap must preserve the business outcome.
                    connection.execute(
                        """
                        UPDATE knowledge_jobs
                        SET status = ?, lease_until = NULL, lease_owner = NULL,
                            last_error = ?, updated_at = ? WHERE id = ?
                        """,
                        (
                            "failed" if run["status"] == "failed" else "completed",
                            (
                                redact_error(run["error_message"])
                                if run["status"] == "failed" else None
                            ),
                            now, row["id"],
                        ),
                    )
                    continue
            exhausted = row["crash_recoveries"] + 1 >= self.max_crash_recoveries
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = ?, crash_recoveries = crash_recoveries + 1,
                    lease_until = NULL, lease_owner = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    "failed" if exhausted else "queued",
                    CRASH_RECOVERY_ERROR_MESSAGE if exhausted else redact_error(row["last_error"]),
                    now, row["id"],
                ),
            )
            if not exhausted:
                continue
            resource_id = row["resource_id"]
            if row["kind"] == "ingestion":
                connection.execute(
                    """
                    UPDATE ingestions SET status = 'interrupted', error = ?, updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (CRASH_RECOVERY_ERROR_MESSAGE, now, resource_id),
                )
            elif row["kind"] == "report":
                connection.execute(
                    """
                    UPDATE reports SET status = 'failed', error = ?, updated_at = ?
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (CRASH_RECOVERY_ERROR_MESSAGE, now, resource_id),
                )
            elif row["kind"] == "agent_run":
                # Agent models depend on KnowledgeRepository during package import.
                from app.agent.repository import AgentRunRepository

                if run is not None and run["status"] in {
                    "created", "queued", "preparing", "running", "validating",
                }:
                    AgentRunRepository(self.database.path, self.database).fail_run_tx(
                        connection, resource_id,
                        error_code=CRASH_RECOVERY_ERROR_CODE,
                        error_message=CRASH_RECOVERY_ERROR_MESSAGE,
                    )
            elif row["kind"] == "research_command":
                connection.execute(
                    """
                    UPDATE research_commands
                    SET status = 'failed', orchestration_stage = 'crash_recovery_exhausted',
                        error = ?, updated_at = ?
                    WHERE id = ? AND status NOT IN ('completed', 'failed')
                    """,
                    (CRASH_RECOVERY_ERROR_MESSAGE, now, resource_id),
                )

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
                    (redact_error(error), _now(), job_id),
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
                        redact_error(error),
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
            crash_recoveries=row["crash_recoveries"],
            priority=row["priority"],
            lease_until=row["lease_until"],
            lease_owner=row["lease_owner"],
            last_error=redact_error(row["last_error"]),
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
