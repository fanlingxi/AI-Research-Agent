from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.error_redaction import redact_error
from app.knowledge.schemas import ProjectionEvent
from app.persistence.sqlite import SQLiteDatabase


class ProjectionRepository:
    """Projection outbox persistence on the shared Knowledge SQLite database.

    Formal publication enqueues events inside the review transaction; executors
    claim, renew and settle them here under lease fencing.  Settling an event
    refreshes its ingestion status in the same transaction and returns the
    ingestion id so the owning repository can read the updated aggregate.
    """

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def enqueue_projection_tx(
        self,
        connection: sqlite3.Connection,
        *,
        ingestion_id: str,
        candidate_id: str,
        aggregate_type: str,
        aggregate_id: str,
        topic_slug: str,
    ) -> None:
        now = _now()
        connection.execute(
            """
            INSERT INTO projection_outbox (
                id, ingestion_id, candidate_id, aggregate_type, aggregate_id,
                topic_slug, status, attempts, lease_until, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, NULL, NULL, ?, ?)
            """,
            (
                f"projection-{uuid4().hex}",
                ingestion_id,
                candidate_id,
                aggregate_type,
                aggregate_id,
                topic_slug,
                now,
                now,
            ),
        )

    def projection_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM projection_outbox GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def projection_status_for_candidate(self, candidate_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM projection_outbox WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return str(row["status"]) if row else "not_required"

    def claim_projection(
        self,
        lease_seconds: int = 120,
        *,
        owner_id: str | None = None,
    ) -> ProjectionEvent | None:
        now = datetime.now(tz=UTC)
        owner = owner_id or f"executor-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'queued', lease_until = NULL, lease_owner = NULL, updated_at = ?
                WHERE status = 'running' AND lease_until < ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            row = connection.execute(
                """
                SELECT * FROM projection_outbox
                WHERE status = 'queued' ORDER BY created_at LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'running', attempts = attempts + 1,
                    lease_until = ?, lease_owner = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lease_until, owner, now.isoformat(), row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM projection_outbox WHERE id = ?", (row["id"],)
            ).fetchone()
        return _projection_from_row(claimed)

    def renew_projection_lease(
        self,
        event_id: str,
        *,
        expected_attempt: int,
        expected_owner: str,
        lease_seconds: int,
    ) -> bool:
        now = datetime.now(tz=UTC)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE projection_outbox SET lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempts = ? AND lease_owner = ?
                """,
                (
                    lease_until,
                    now.isoformat(),
                    event_id,
                    expected_attempt,
                    expected_owner,
                ),
            )
        return updated.rowcount == 1

    def complete_projection(
        self,
        event_id: str,
        *,
        expected_attempt: int | None = None,
        expected_owner: str | None = None,
    ) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT ingestion_id FROM projection_outbox WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            updated = connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'completed', lease_until = NULL, lease_owner = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                  AND (? IS NULL OR (status = 'running' AND attempts = ? AND lease_owner = ?))
                """,
                (
                    _now(),
                    event_id,
                    expected_attempt,
                    expected_attempt,
                    expected_owner,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"Projection {event_id} is no longer owned by this executor.")
            ingestion_id = str(row["ingestion_id"])
            refresh_ingestion_status_tx(connection, ingestion_id, now=_now())
        return ingestion_id

    def fail_projection(
        self,
        event_id: str,
        error: str,
        *,
        expected_attempt: int | None = None,
        expected_owner: str | None = None,
    ) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT ingestion_id FROM projection_outbox WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            ingestion_id = str(row["ingestion_id"])
            updated = connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'failed', lease_until = NULL, lease_owner = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ?
                  AND (? IS NULL OR (status = 'running' AND attempts = ? AND lease_owner = ?))
                """,
                (
                    redact_error(error),
                    _now(),
                    event_id,
                    expected_attempt,
                    expected_attempt,
                    expected_owner,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError(f"Projection {event_id} is no longer owned by this executor.")
            refresh_ingestion_status_tx(
                connection,
                ingestion_id,
                now=_now(),
                projection_error=f"正式投影失败：{error}"[:4000],
            )
        return ingestion_id

    def retry_projection(self, event_id: str) -> ProjectionEvent:
        """Safely requeue one failed outbox event without bypassing its resource state."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT ingestion_id, status FROM projection_outbox WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row["status"] != "failed":
                raise ValueError("Only a failed projection can be retried.")
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'queued', lease_until = NULL, lease_owner = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (now, event_id),
            )
            connection.execute(
                """
                UPDATE ingestions
                SET status = 'publishing', error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, row["ingestion_id"]),
            )
            refreshed = connection.execute(
                "SELECT * FROM projection_outbox WHERE id = ?", (event_id,)
            ).fetchone()
        return _projection_from_row(refreshed)


def refresh_ingestion_status_tx(
    connection: sqlite3.Connection,
    ingestion_id: str,
    *,
    now: str,
    projection_error: str | None = None,
) -> None:
    ingestion = connection.execute(
        "SELECT status, error FROM ingestions WHERE id = ?", (ingestion_id,)
    ).fetchone()
    if ingestion is None:
        raise KeyError(ingestion_id)
    if str(ingestion["status"]) in {"queued", "running"}:
        return
    draft_count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM candidates
            WHERE ingestion_id = ? AND status IN ('draft', 'approved')
            """,
            (ingestion_id,),
        ).fetchone()[0]
    )
    pending = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM projection_outbox
            WHERE ingestion_id = ? AND status IN ('queued', 'running')
            """,
            (ingestion_id,),
        ).fetchone()[0]
    )
    failed = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM projection_outbox
            WHERE ingestion_id = ? AND status = 'failed'
            """,
            (ingestion_id,),
        ).fetchone()[0]
    )
    if projection_error is not None:
        status = "failed"
    elif draft_count:
        status = "needs_review"
    elif failed:
        status = "failed"
    elif pending:
        status = "publishing"
    else:
        status = "completed"
    error = redact_error(projection_error if projection_error is not None else ingestion["error"])
    connection.execute(
        "UPDATE ingestions SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (status, error, now, ingestion_id),
    )


def _projection_from_row(row: sqlite3.Row) -> ProjectionEvent:
    return ProjectionEvent(
        id=row["id"],
        ingestion_id=row["ingestion_id"],
        candidate_id=row["candidate_id"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        topic_slug=row["topic_slug"],
        status=row["status"],
        attempts=row["attempts"],
        lease_until=row["lease_until"],
        lease_owner=row["lease_owner"],
        last_error=redact_error(row["last_error"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
