"""SQLite persistence boundary for durable Agent Runtime audit records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.agent.errors import (
    AgentRunConflictError,
    AgentRunNotRecoverableError,
    AgentRunTerminalError,
)
from app.agent.models import AgentRun, AgentRunEvent, AgentRunOptions, AgentRunOutput, AgentToolCall
from app.domain_plugins.contracts import PluginPin
from app.execution import ExecutionFence
from app.persistence.sqlite import SQLiteDatabase

_TERMINAL_STATUSES = {"completed", "cancelled", "needs_review", "stale_context"}
_RECOVERABLE_INTERRUPTED_STATUSES = {"preparing", "running", "validating"}


class AgentRunRepository:
    """Own the authoritative AgentRun, Event, ToolCall, and Output rows."""

    def __init__(self, path: str, database: SQLiteDatabase | None = None) -> None:
        self.path = path
        self.database = database or SQLiteDatabase(path)

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def create_run_tx(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        task_id: str,
        context_snapshot_id: str,
        context_sha256: str,
        workflow_name: str,
        workflow_version: str,
        plugin: PluginPin,
        options: AgentRunOptions,
        model_provider: str,
        model_name: str,
        max_steps: int,
        max_tool_calls: int,
        token_budget: int,
        run_id: str | None = None,
    ) -> AgentRun:
        self._require_context_snapshot_tx(
            connection,
            context_snapshot_id=context_snapshot_id,
            project_id=project_id,
            task_id=task_id,
            context_sha256=context_sha256,
        )
        resolved_run_id = run_id or f"agent-run-{uuid4().hex}"
        now = _now()
        try:
            connection.execute(
                """
                INSERT INTO agent_runs (
                    id, project_id, task_id, context_snapshot_id, context_sha256,
                    workflow_name, workflow_version, plugin_key, plugin_version,
                    plugin_contract_version, plugin_workflow_key, options_json, status,
                    model_provider,
                    model_name,
                    max_steps, max_tool_calls, token_budget, tool_call_count, repair_count,
                    current_node, error_code, error_message, started_at, completed_at,
                    created_at, updated_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, 0, 0,
                          NULL, NULL, NULL, NULL, NULL, ?, ?, 1)
                """,
                (
                    resolved_run_id,
                    project_id,
                    task_id,
                    context_snapshot_id,
                    context_sha256,
                    workflow_name,
                    workflow_version,
                    plugin.key,
                    plugin.version,
                    plugin.contract_version,
                    plugin.workflow_key,
                    _dump(options.model_dump(mode="json")),
                    model_provider,
                    model_name,
                    max_steps,
                    max_tool_calls,
                    token_budget,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "agent_runs_one_active_task_idx" in str(exc) or "agent_runs.task_id" in str(exc):
                raise AgentRunConflictError(
                    "WorkspaceTask already has an active AgentRun."
                ) from exc
            raise
        self._append_event_tx(
            connection,
            run_id=resolved_run_id,
            event_type="run_created",
            node_name=None,
            status="created",
            output_summary={"context_snapshot_id": context_snapshot_id},
        )
        return self._run_from_row(self._require_run_tx(connection, resolved_run_id))

    def queue_run_tx(self, connection: sqlite3.Connection, run_id: str) -> AgentRun:
        row = self._require_run_tx(connection, run_id)
        self._require_status(row, {"created"})
        connection.execute(
            """
            UPDATE agent_runs
            SET status = 'queued', current_node = NULL, error_code = NULL, error_message = NULL,
                updated_at = ?, revision = revision + 1
            WHERE id = ?
            """,
            (_now(), run_id),
        )
        self._append_event_tx(
            connection,
            run_id=run_id,
            event_type="run_queued",
            node_name=None,
            status="queued",
        )
        return self._run_from_row(self._require_run_tx(connection, run_id))

    def mark_node(
        self,
        run_id: str,
        *,
        status: str,
        node_name: str,
        input_summary: dict[str, Any] | None = None,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.mark_node_tx(
                connection,
                run_id,
                status=status,
                node_name=node_name,
                input_summary=input_summary,
                execution_fence=execution_fence,
            )

    def mark_node_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        status: str,
        node_name: str,
        input_summary: dict[str, Any] | None = None,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        self._require_execution_fence_tx(connection, run_id, execution_fence)
        row = self._require_run_tx(connection, run_id)
        allowed_previous = {
            "preparing": {"queued"},
            # The only legal validation -> running path is begin_repair(),
            # which atomically consumes the one permitted repair attempt.
            "running": {"preparing", "running"},
            "validating": {"running"},
        }
        self._require_status(row, allowed_previous.get(status, set()))
        now = _now()
        connection.execute(
            """
            UPDATE agent_runs
            SET status = ?, current_node = ?, started_at = COALESCE(started_at, ?),
                updated_at = ?, revision = revision + 1
            WHERE id = ?
            """,
            (status, node_name, now, now, run_id),
        )
        self._append_event_tx(
            connection,
            run_id=run_id,
            event_type="node_started",
            node_name=node_name,
            status=status,
            input_summary=input_summary,
        )
        return self._run_from_row(self._require_run_tx(connection, run_id))

    def record_node_completed(
        self,
        run_id: str,
        *,
        node_name: str,
        output_summary: dict[str, Any],
        token_usage: dict[str, Any] | None = None,
        latency_ms: float | None = None,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRunEvent:
        """Append bounded node telemetry without retaining prompts or draft bodies."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_execution_fence_tx(connection, run_id, execution_fence)
            row = self._require_run_tx(connection, run_id)
            self._require_not_terminal(row)
            usage = token_usage or {}
            requested_tokens = int(usage.get("input_tokens", 0) or 0) + int(
                usage.get("output_tokens", 0) or 0
            )
            used_tokens = int(
                connection.execute(
                    """
                    SELECT COALESCE(
                        SUM(
                            COALESCE(json_extract(token_usage_json, '$.input_tokens'), 0)
                            + COALESCE(json_extract(token_usage_json, '$.output_tokens'), 0)
                        ),
                        0
                    )
                    FROM agent_run_events WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()[0]
            )
            if used_tokens + requested_tokens > int(row["token_budget"]):
                raise AgentRunConflictError("AgentRun has exhausted its token budget.")
            return self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="node_completed",
                node_name=node_name,
                status=str(row["status"]),
                output_summary=output_summary,
                token_usage=usage,
                latency_ms=latency_ms,
            )

    def record_tool_call(
        self,
        run_id: str,
        *,
        sequence: int,
        tool_name: str,
        permission: str,
        arguments: dict[str, Any],
        result_summary: dict[str, Any],
        idempotency_key: str,
        node_name: str = "inspect_context",
        execution_fence: ExecutionFence | None = None,
    ) -> AgentToolCall:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.record_tool_call_tx(
                connection,
                run_id,
                sequence=sequence,
                tool_name=tool_name,
                permission=permission,
                arguments=arguments,
                result_summary=result_summary,
                idempotency_key=idempotency_key,
                node_name=node_name,
                execution_fence=execution_fence,
            )

    def record_tool_call_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        sequence: int,
        tool_name: str,
        permission: str,
        arguments: dict[str, Any],
        result_summary: dict[str, Any],
        idempotency_key: str,
        node_name: str = "inspect_context",
        execution_fence: ExecutionFence | None = None,
    ) -> AgentToolCall:
        self._require_execution_fence_tx(connection, run_id, execution_fence)
        existing = connection.execute(
            "SELECT * FROM agent_tool_calls WHERE run_id = ? AND idempotency_key = ?",
            (run_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            return self._tool_call_from_row(existing)
        run = self._require_run_tx(connection, run_id)
        self._require_status(run, {"running"})
        if int(run["tool_call_count"]) >= int(run["max_tool_calls"]):
            raise AgentRunConflictError("AgentRun has exhausted its tool call budget.")
        event = self._append_event_tx(
            connection,
            run_id=run_id,
            event_type="tool_completed",
            node_name=node_name,
            status="running",
            input_summary={"tool_name": tool_name, "permission": permission},
            output_summary=result_summary,
        )
        now = _now()
        result_hash = _sha256(result_summary)
        tool_id = f"agent-tool-call-{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO agent_tool_calls (
                id, run_id, event_id, sequence, tool_name, permission, arguments_json,
                result_summary_json, result_hash, status, idempotency_key, started_at,
                completed_at, error_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, '{}')
            """,
            (
                tool_id,
                run_id,
                event.id,
                sequence,
                tool_name,
                permission,
                _dump(arguments),
                _dump(result_summary),
                result_hash,
                idempotency_key,
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE agent_runs
            SET tool_call_count = tool_call_count + 1, updated_at = ?, revision = revision + 1
            WHERE id = ?
            """,
            (now, run_id),
        )
        return self._tool_call_from_row(
            connection.execute("SELECT * FROM agent_tool_calls WHERE id = ?", (tool_id,)).fetchone()
        )

    def complete_with_output(
        self,
        run_id: str,
        *,
        output_type: str,
        structured: dict[str, Any],
        rendered_text: str,
        validation: dict[str, Any],
        output_id: str | None = None,
        completion_summary: dict[str, Any] | None = None,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRunOutput:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.complete_with_output_tx(
                connection,
                run_id,
                output_type=output_type,
                structured=structured,
                rendered_text=rendered_text,
                validation=validation,
                output_id=output_id,
                completion_summary=completion_summary,
                execution_fence=execution_fence,
            )

    def complete_with_output_tx(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        output_type: str,
        structured: dict[str, Any],
        rendered_text: str,
        validation: dict[str, Any],
        output_id: str | None = None,
        completion_summary: dict[str, Any] | None = None,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRunOutput:
        self._require_execution_fence_tx(connection, run_id, execution_fence)
        run = self._require_run_tx(connection, run_id)
        self._require_status(run, {"validating"})
        existing = connection.execute(
            "SELECT * FROM agent_run_outputs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            return self._output_from_row(existing)
        now = _now()
        output_id = output_id or f"agent-output-{uuid4().hex}"
        output_hash = _sha256(
            {"output_type": output_type, "structured": structured, "rendered_text": rendered_text}
        )
        connection.execute(
            """
            INSERT INTO agent_run_outputs (
                id, run_id, output_type, structured_json, rendered_text, output_sha256,
                validation_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                output_id,
                run_id,
                output_type,
                _dump(structured),
                rendered_text,
                output_hash,
                _dump(validation),
                now,
            ),
        )
        connection.execute(
            """
            UPDATE agent_runs
            SET status = 'completed', current_node = 'complete', completed_at = ?, updated_at = ?,
                revision = revision + 1
            WHERE id = ?
            """,
            (now, now, run_id),
        )
        self._append_event_tx(
            connection,
            run_id=run_id,
            event_type="run_completed",
            node_name="complete",
            status="completed",
            output_summary={
                "output_id": output_id,
                "output_sha256": output_hash,
                **(completion_summary or {}),
            },
        )
        output_row = connection.execute(
            "SELECT * FROM agent_run_outputs WHERE id = ?", (output_id,)
        ).fetchone()
        return self._output_from_row(output_row)

    def fail_run(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_execution_fence_tx(connection, run_id, execution_fence)
            row = self._require_run_tx(connection, run_id)
            if str(row["status"]) in _TERMINAL_STATUSES:
                return self._run_from_row(row)
            now = _now()
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'failed', error_code = ?, error_message = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (error_code, error_message[:4000], now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="run_failed",
                node_name=str(row["current_node"] or "runtime"),
                status="failed",
                error={"code": error_code, "message": error_message[:4000]},
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def cancel_run(self, run_id: str) -> AgentRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run_tx(connection, run_id)
            self._require_not_terminal(row)
            now = _now()
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'cancelled', completed_at = ?, updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (now, now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="run_cancelled",
                node_name=str(row["current_node"] or "runtime"),
                status="cancelled",
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def close_needs_review(self, run_id: str) -> AgentRun:
        """Close an invalid reviewed run without accepting its draft."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_run_tx(connection, run_id)
            self._require_status(row, {"needs_review"})
            now = _now()
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'cancelled', completed_at = ?, updated_at = ?,
                    revision = revision + 1
                WHERE id = ?
                """,
                (now, now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="review_closed",
                node_name="citation_validation",
                status="cancelled",
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def mark_stale_context_if_needed(
        self,
        run_id: str,
        *,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        """Atomically compare snapshot revisions and stop stale queued work."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_execution_fence_tx(connection, run_id, execution_fence)
            row = self._require_run_tx(connection, run_id)
            self._require_status(row, {"queued"})
            revisions = connection.execute(
                """
                SELECT cs.project_revision AS snapshot_project_revision,
                       cs.task_revision AS snapshot_task_revision,
                       p.revision AS current_project_revision,
                       t.revision AS current_task_revision
                FROM context_snapshots cs
                JOIN projects p ON p.id = cs.project_id
                JOIN workspace_tasks t ON t.id = cs.task_id
                WHERE cs.id = ? AND cs.project_id = ? AND cs.task_id = ?
                """,
                (row["context_snapshot_id"], row["project_id"], row["task_id"]),
            ).fetchone()
            if revisions is None:
                raise AgentRunConflictError(
                    "AgentRun ContextSnapshot no longer matches its Project and Task."
                )
            snapshot_project = int(revisions["snapshot_project_revision"])
            snapshot_task = int(revisions["snapshot_task_revision"])
            current_project = int(revisions["current_project_revision"])
            current_task = int(revisions["current_task_revision"])
            if snapshot_project == current_project and snapshot_task == current_task:
                return self._run_from_row(row)

            now = _now()
            message = (
                "ContextSnapshot revisions are stale: "
                f"Project {snapshot_project}->{current_project}, "
                f"Task {snapshot_task}->{current_task}. Create a new snapshot and AgentRun."
            )
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'stale_context', current_node = 'context_revision_check',
                    error_code = 'stale_context', error_message = ?, completed_at = ?,
                    updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (message, now, now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="run_stale_context",
                node_name="context_revision_check",
                status="stale_context",
                output_summary={
                    "snapshot_project_revision": snapshot_project,
                    "current_project_revision": current_project,
                    "snapshot_task_revision": snapshot_task,
                    "current_task_revision": current_task,
                },
                error={"code": "stale_context", "message": message},
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def begin_repair(
        self,
        run_id: str,
        *,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        """Consume the single permitted research-draft repair attempt."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_execution_fence_tx(connection, run_id, execution_fence)
            row = self._require_run_tx(connection, run_id)
            self._require_status(row, {"validating"})
            if int(row["repair_count"]) >= 1:
                raise AgentRunConflictError("AgentRun has exhausted its repair budget.")
            now = _now()
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'running', current_node = 'repair', repair_count = repair_count + 1,
                    updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="repair_started",
                node_name="repair",
                status="running",
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def mark_needs_review(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message: str,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        """Stop after the bounded repair path without creating final records."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_execution_fence_tx(connection, run_id, execution_fence)
            row = self._require_run_tx(connection, run_id)
            self._require_status(row, {"validating"})
            now = _now()
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'needs_review', current_node = 'citation_validation',
                    error_code = ?, error_message = ?, updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (error_code, error_message[:4000], now, run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="run_needs_review",
                node_name="citation_validation",
                status="needs_review",
                error={"code": error_code, "message": error_message[:4000]},
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def recover_interrupted_run(
        self,
        run_id: str,
        *,
        execution_fence: ExecutionFence | None = None,
    ) -> AgentRun:
        """Return a pre-checkpoint-crash run to the only executable state.

        This is intentionally distinct from manual failed-run recovery: it is
        only for a durable job that was reclaimed after a worker interruption.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_execution_fence_tx(connection, run_id, execution_fence)
            row = self._require_run_tx(connection, run_id)
            self._require_status(row, _RECOVERABLE_INTERRUPTED_STATUSES)
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'queued', current_node = NULL, updated_at = ?, revision = revision + 1
                WHERE id = ?
                """,
                (_now(), run_id),
            )
            self._append_event_tx(
                connection,
                run_id=run_id,
                event_type="run_recovered",
                node_name=str(row["current_node"] or "runtime"),
                status="queued",
            )
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def resume_run_tx(self, connection: sqlite3.Connection, run_id: str) -> AgentRun:
        row = self._require_run_tx(connection, run_id)
        if str(row["status"]) != "failed":
            raise AgentRunNotRecoverableError("Only failed AgentRuns can be manually resumed.")
        connection.execute(
            """
            UPDATE agent_runs
            SET status = 'queued', error_code = NULL, error_message = NULL, updated_at = ?,
                revision = revision + 1
            WHERE id = ?
            """,
            (_now(), run_id),
        )
        self._append_event_tx(
            connection,
            run_id=run_id,
            event_type="run_resumed",
            node_name=str(row["current_node"] or "runtime"),
            status="queued",
        )
        return self._run_from_row(self._require_run_tx(connection, run_id))

    def get_run(self, run_id: str) -> AgentRun:
        with self._connect() as connection:
            return self._run_from_row(self._require_run_tx(connection, run_id))

    def get_run_tx(self, connection: sqlite3.Connection, run_id: str) -> AgentRun:
        """Read one run using a caller-owned transaction."""

        return self._run_from_row(self._require_run_tx(connection, run_id))

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[AgentRun], str | None]:
        """Return recent business runs using an opaque, stable keyset cursor."""

        if limit < 1:
            raise ValueError("AgentRun page limit must be positive.")
        if task_id is not None and project_id is None:
            raise ValueError("AgentRun task filters require a Project filter.")
        cursor_values = _decode_run_cursor(cursor) if cursor else None
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if cursor_values is not None:
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            params.extend((cursor_values[0], cursor_values[0], cursor_values[1]))
        query = "SELECT * FROM agent_runs"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_cursor = (
            _encode_run_cursor(str(selected[-1]["created_at"]), str(selected[-1]["id"]))
            if has_more and selected
            else None
        )
        return [self._run_from_row(row) for row in selected], next_cursor

    def list_events(self, run_id: str) -> list[AgentRunEvent]:
        with self._connect() as connection:
            self._require_run_tx(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM agent_run_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_events_page(
        self, run_id: str, *, after_sequence: int, limit: int
    ) -> tuple[list[AgentRunEvent], int | None]:
        """Return a forward-only page of append-only audit events."""

        if after_sequence < 0:
            raise ValueError("AgentRun event cursor cannot be negative.")
        if limit < 1:
            raise ValueError("AgentRun event page limit must be positive.")
        with self._connect() as connection:
            self._require_run_tx(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM agent_run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, after_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        next_sequence = int(selected[-1]["sequence"]) if has_more and selected else None
        return [self._event_from_row(row) for row in selected], next_sequence

    def list_tool_calls(self, run_id: str) -> list[AgentToolCall]:
        with self._connect() as connection:
            self._require_run_tx(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM agent_tool_calls WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        return [self._tool_call_from_row(row) for row in rows]

    def list_tool_calls_for_events(
        self, run_id: str, event_ids: list[str]
    ) -> list[AgentToolCall]:
        """Return only tool calls attached to one displayed trace page."""

        if not event_ids:
            return []
        placeholders = ", ".join("?" for _ in event_ids)
        with self._connect() as connection:
            self._require_run_tx(connection, run_id)
            rows = connection.execute(
                f"""
                SELECT * FROM agent_tool_calls
                WHERE run_id = ? AND event_id IN ({placeholders})
                ORDER BY sequence
                """,
                (run_id, *event_ids),
            ).fetchall()
        return [self._tool_call_from_row(row) for row in rows]

    def trace_totals(self, run_id: str) -> tuple[dict[str, int], float]:
        """Aggregate bounded telemetry without loading every event into the UI."""

        with self._connect() as connection:
            self._require_run_tx(connection, run_id)
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(COALESCE(json_extract(token_usage_json, '$.input_tokens'), 0)), 0)
                        AS input_tokens,
                    COALESCE(SUM(COALESCE(json_extract(token_usage_json, '$.output_tokens'), 0)), 0)
                        AS output_tokens,
                    COALESCE(SUM(latency_ms), 0) AS latency_ms
                FROM agent_run_events
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        input_tokens = int(row["input_tokens"])
        output_tokens = int(row["output_tokens"])
        return (
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            float(row["latency_ms"]),
        )

    def get_output(self, run_id: str) -> AgentRunOutput:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_run_outputs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"AgentRun {run_id} has no output")
        return self._output_from_row(row)

    def _append_event_tx(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        node_name: str | None,
        status: str,
        input_summary: dict[str, Any] | None = None,
        output_summary: dict[str, Any] | None = None,
        token_usage: dict[str, Any] | None = None,
        latency_ms: float | None = None,
        error: dict[str, Any] | None = None,
    ) -> AgentRunEvent:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        event_id = f"agent-event-{uuid4().hex}"
        now = _now()
        connection.execute(
            """
            INSERT INTO agent_run_events (
                id, run_id, sequence, event_type, node_name, status, input_summary_json,
                output_summary_json, token_usage_json, latency_ms, error_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                sequence,
                event_type,
                node_name,
                status,
                _dump(input_summary or {}),
                _dump(output_summary or {}),
                _dump(token_usage or {}),
                latency_ms,
                _dump(error or {}),
                now,
            ),
        )
        event_row = connection.execute(
            "SELECT * FROM agent_run_events WHERE id = ?", (event_id,)
        ).fetchone()
        return self._event_from_row(event_row)

    @staticmethod
    def _require_context_snapshot_tx(
        connection: sqlite3.Connection,
        *,
        context_snapshot_id: str,
        project_id: str,
        task_id: str,
        context_sha256: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT project_id, task_id, package_sha256 FROM context_snapshots WHERE id = ?
            """,
            (context_snapshot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"ContextSnapshot {context_snapshot_id} not found")
        if (
            str(row["project_id"]) != project_id
            or str(row["task_id"]) != task_id
            or str(row["package_sha256"]) != context_sha256
        ):
            raise AgentRunConflictError(
                "ContextSnapshot does not match this Project, Task, or hash."
            )

    @staticmethod
    def _require_run_tx(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"AgentRun {run_id} not found")
        return row

    @staticmethod
    def _require_execution_fence_tx(
        connection: sqlite3.Connection,
        run_id: str,
        fence: ExecutionFence | None,
    ) -> None:
        if fence is None:
            return
        if fence.kind != "agent_run" or fence.resource_id != run_id:
            raise AgentRunConflictError("AgentRun execution fence does not match the run.")
        owned = connection.execute(
            """
            SELECT 1 FROM knowledge_jobs
            WHERE id = ? AND kind = 'agent_run' AND resource_id = ?
              AND status = 'running' AND attempts = ? AND lease_owner = ?
            """,
            (fence.job_id, run_id, fence.attempt, fence.owner_id),
        ).fetchone()
        if owned is None:
            raise AgentRunConflictError(
                f"AgentRun {run_id} is no longer owned by attempt {fence.attempt}."
            )

    @staticmethod
    def _require_not_terminal(row: sqlite3.Row) -> None:
        if str(row["status"]) in _TERMINAL_STATUSES:
            raise AgentRunTerminalError(f"AgentRun is {row['status']}.")

    @staticmethod
    def _require_status(row: sqlite3.Row, expected: set[str]) -> None:
        if str(row["status"]) not in expected:
            allowed = ", ".join(sorted(expected))
            raise AgentRunTerminalError(
                f"AgentRun is {row['status']}; expected one of: {allowed}."
            )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> AgentRun:
        return AgentRun(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            context_snapshot_id=row["context_snapshot_id"],
            context_sha256=row["context_sha256"],
            workflow_name=row["workflow_name"],
            workflow_version=row["workflow_version"],
            plugin=PluginPin(
                key=row["plugin_key"],
                version=row["plugin_version"],
                contract_version=row["plugin_contract_version"],
                workflow_key=row["plugin_workflow_key"],
            ),
            options=AgentRunOptions.model_validate(json.loads(row["options_json"] or "{}")),
            status=row["status"],
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            max_steps=row["max_steps"],
            max_tool_calls=row["max_tool_calls"],
            token_budget=row["token_budget"],
            tool_call_count=row["tool_call_count"],
            repair_count=row["repair_count"],
            current_node=row["current_node"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=row["revision"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AgentRunEvent:
        return AgentRunEvent(
            id=row["id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            node_name=row["node_name"],
            status=row["status"],
            input_summary=json.loads(row["input_summary_json"] or "{}"),
            output_summary=json.loads(row["output_summary_json"] or "{}"),
            token_usage=json.loads(row["token_usage_json"] or "{}"),
            latency_ms=row["latency_ms"],
            error=json.loads(row["error_json"] or "{}"),
            created_at=row["created_at"],
        )

    @staticmethod
    def _tool_call_from_row(row: sqlite3.Row) -> AgentToolCall:
        return AgentToolCall(
            id=row["id"],
            run_id=row["run_id"],
            event_id=row["event_id"],
            sequence=row["sequence"],
            tool_name=row["tool_name"],
            permission=row["permission"],
            arguments=json.loads(row["arguments_json"] or "{}"),
            result_summary=json.loads(row["result_summary_json"] or "{}"),
            result_hash=row["result_hash"],
            status=row["status"],
            idempotency_key=row["idempotency_key"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=json.loads(row["error_json"] or "{}"),
        )

    @staticmethod
    def _output_from_row(row: sqlite3.Row) -> AgentRunOutput:
        return AgentRunOutput(
            id=row["id"],
            run_id=row["run_id"],
            output_type=row["output_type"],
            structured=json.loads(row["structured_json"]),
            rendered_text=row["rendered_text"],
            output_sha256=row["output_sha256"],
            validation=json.loads(row["validation_json"] or "{}"),
            created_at=row["created_at"],
        )


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _encode_run_cursor(created_at: str, run_id: str) -> str:
    """Encode the ordered AgentRun key without exposing a query fragment."""

    payload = json.dumps([created_at, run_id], separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_run_cursor(cursor: str) -> tuple[str, str]:
    """Validate an opaque keyset cursor supplied by a UI client."""

    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, run_id = json.loads(urlsafe_b64decode(padded.encode("ascii")))
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise ValueError("AgentRun cursor is invalid.") from exc
    if not isinstance(created_at, str) or not isinstance(run_id, str):
        raise ValueError("AgentRun cursor is invalid.")
    return created_at, run_id
