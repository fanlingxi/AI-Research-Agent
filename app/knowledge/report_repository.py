from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from app.knowledge.core_repository import KnowledgeCoreRepository
from app.knowledge.schemas import KnowledgeJob, ReportEvaluation, ReportEvidence, ResearchReport
from app.persistence.sqlite import SQLiteDatabase
from app.runtime.job_repository import JobRepository


class StaleReportExecution(RuntimeError):
    """Raised when an expired report executor tries to write after a newer claim."""


class ReportRepository:
    """Report lifecycle persistence, including atomic report and job transitions.

    Uses the shared database, queue and Knowledge Core to validate input versions
    before committing a terminal result. Model calls belong to the report service.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        jobs: JobRepository,
        core_repository: KnowledgeCoreRepository,
    ) -> None:
        self.database = database
        self.jobs = jobs
        self.core_repository = core_repository

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def mark_report_for_dispatch(self, report_id: str) -> ResearchReport:
        """Persist the user's intent for the independent durable Worker."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute(
                "SELECT status, run_metadata_json FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            if report is None:
                raise KeyError(report_id)
            if report["status"] not in {"queued", "running"}:
                raise ValueError(f"Report cannot be dispatched from {report['status']}.")
            job = connection.execute(
                """
                SELECT id, payload_json FROM knowledge_jobs
                WHERE kind = 'report' AND resource_id = ?
                """,
                (report_id,),
            ).fetchone()
            if job is None:
                raise KeyError(report_id)
            payload = json.loads(job["payload_json"] or "{}")
            payload["auto_execute"] = True
            connection.execute(
                """
                UPDATE knowledge_jobs SET payload_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_dump(payload), _now(), job["id"]),
            )
            connection.execute(
                "UPDATE knowledge_jobs SET priority = MAX(priority, 100) WHERE id = ?",
                (job["id"],),
            )
            if report["status"] == "queued":
                metadata = json.loads(report["run_metadata_json"] or "{}")
                metadata["current_stage"] = "queued_for_dispatch"
                connection.execute(
                    """
                    UPDATE reports SET run_metadata_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (_dump(metadata), _now(), report_id),
                )
        return self.get_report(report_id)

    def claim_dispatched_report_job(
        self,
        lease_seconds: int = 120,
        *,
        owner_id: str | None = None,
    ) -> KnowledgeJob | None:
        """Claim the next report explicitly marked for built-in durable dispatch."""
        now = datetime.now(tz=UTC)
        now_text = now.isoformat()
        owner = owner_id or f"executor-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job.* FROM knowledge_jobs AS job
                JOIN reports AS report ON report.id = job.resource_id
                WHERE job.kind = 'report'
                  AND job.status IN ('queued', 'running')
                  AND report.status IN ('queued', 'running')
                ORDER BY job.created_at, job.id
                """
            ).fetchall()
            selected = None
            for row in rows:
                payload = json.loads(row["payload_json"] or "{}")
                if not payload.get("auto_execute"):
                    continue
                status = str(row["status"])
                expired = status == "running" and (
                    row["lease_until"] is None or str(row["lease_until"]) < now_text
                )
                if expired:
                    connection.execute(
                        """
                        UPDATE knowledge_jobs
                        SET status = 'queued', lease_until = NULL,
                            lease_owner = NULL, updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (now_text, row["id"]),
                    )
                    connection.execute(
                        """
                        UPDATE reports SET status = 'queued', updated_at = ?
                        WHERE id = ? AND status = 'running'
                        """,
                        (now_text, row["resource_id"]),
                    )
                    status = "queued"
                if status == "queued":
                    selected = row
                    break
            if selected is None:
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            updated = connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'running', attempts = attempts + 1,
                    lease_until = ?, lease_owner = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lease_until, owner, now_text, selected["id"]),
            )
            if updated.rowcount != 1:
                return None
            report_row = connection.execute(
                "SELECT run_metadata_json FROM reports WHERE id = ?",
                (selected["resource_id"],),
            ).fetchone()
            metadata = json.loads(report_row["run_metadata_json"] or "{}")
            metadata["current_stage"] = "scheduled"
            connection.execute(
                """
                UPDATE reports
                SET status = 'running', run_metadata_json = ?, error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_dump(metadata), now_text, selected["resource_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (selected["id"],)
            ).fetchone()
        return self.jobs.job_from_row(claimed)

    def create_report(
        self,
        *,
        query: str,
        topic_slugs: list[str],
        top_k: int,
        report_depth: str,
        run_metadata: dict[str, Any] | None = None,
        auto_execute: bool = False,
        report_id: str | None = None,
    ) -> ResearchReport:
        resolved_report_id = report_id or f"report-{uuid4().hex}"
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO reports (
                    id, query, topic_slugs_json, top_k, report_depth, status,
                    content, evidence_json, evaluation_json, run_metadata_json,
                    error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', '', '[]', NULL, ?, NULL, ?, ?)
                """,
                (
                    resolved_report_id,
                    query.strip(),
                    _dump(topic_slugs),
                    top_k,
                    report_depth,
                    _dump(run_metadata or {}),
                    now,
                    now,
                ),
            )
            self.jobs.enqueue_job_tx(
                connection,
                kind="report",
                resource_id=resolved_report_id,
                payload={"auto_execute": True} if auto_execute else {},
                priority=100 if auto_execute else 0,
            )
        return self.get_report(resolved_report_id)

    def list_reports(self, limit: int = 100) -> list[ResearchReport]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._report_from_row(row) for row in rows]

    def get_report(self, report_id: str) -> ResearchReport:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if row is None:
            raise KeyError(report_id)
        return self._report_from_row(row)

    def reset_report_for_retry(
        self,
        report_id: str,
        *,
        auto_execute: bool = False,
    ) -> ResearchReport:
        """Reset a failed report and its durable job without changing its identity."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute(
                "SELECT status, run_metadata_json FROM reports WHERE id = ?", (report_id,)
            ).fetchone()
            if report is None:
                raise KeyError(report_id)
            if report["status"] != "failed":
                raise ValueError(
                    f"Only failed reports can be retried; report is {report['status']}."
                )
            job = connection.execute(
                """
                SELECT id, status FROM knowledge_jobs
                WHERE kind = 'report' AND resource_id = ?
                """,
                (report_id,),
            ).fetchone()
            if job is None:
                raise KeyError(report_id)
            if job["status"] == "running":
                raise ValueError("The report job is already running.")
            if job["status"] not in {"failed", "queued"}:
                raise ValueError(f"The report job cannot be retried from {job['status']}.")
            metadata = json.loads(report["run_metadata_json"] or "{}")
            metadata.pop("current_stage", None)
            if auto_execute:
                metadata["current_stage"] = "queued_for_dispatch"
            connection.execute(
                """
                UPDATE reports
                SET status = 'queued', content = '', evidence_json = '[]',
                    evaluation_json = NULL, run_metadata_json = ?, error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (_dump(metadata), now, report_id),
            )
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'queued', payload_json = ?, priority = ?,
                    lease_until = NULL, lease_owner = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    _dump({"auto_execute": True}) if auto_execute else "{}",
                    100 if auto_execute else 0,
                    now,
                    job["id"],
                ),
            )
        return self.get_report(report_id)

    def finalize_report_execution(
        self,
        report_id: str,
        job_id: str,
        *,
        expected_attempt: int,
        status: Literal["completed", "failed"],
        content: str | None = None,
        evidence: list[ReportEvidence] | None = None,
        evaluation: ReportEvaluation | None = None,
        run_metadata: dict[str, Any] | None = None,
        error: str | None = None,
        expected_owner: str | None = None,
        expected_request: dict[str, Any] | None = None,
        read_guard: dict[str, Any] | None = None,
    ) -> ResearchReport:
        """Fence and commit a report plus its durable job as one terminal write."""
        if status == "completed" and (expected_request is None or read_guard is None):
            raise ValueError("Completed reports require their original request and input guard.")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if row is None:
                raise KeyError(report_id)
            report = self._report_from_row(row)
            owned = connection.execute(
                """
                SELECT 1 FROM knowledge_jobs
                WHERE id = ? AND kind = 'report' AND resource_id = ?
                  AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (job_id, report_id, expected_attempt, expected_owner, expected_owner),
            ).fetchone()
            if owned is None:
                raise StaleReportExecution(
                    f"Report {report_id} is no longer owned by attempt {expected_attempt}."
                )
            if report.status not in {"queued", "running"}:
                raise StaleReportExecution(f"Report {report_id} is already terminal.")
            if expected_request is not None:
                from app.knowledge.report_inputs import report_request, validate_read_guard_tx

                rejection = None
                if report_request(report) != expected_request:
                    rejection = "report_request_changed"
                elif read_guard is not None and (
                    read_guard.get("topic_slugs") != expected_request["topic_slugs"]
                    or not validate_read_guard_tx(connection, self.core_repository, read_guard)
                ):
                    rejection = "report_knowledge_changed"
                if rejection:
                    status, content = "failed", ""
                    if evaluation is not None:
                        evaluation = evaluation.model_copy(update={"passed": False})
                    error = f"{rejection}: 报告输入已失效，请重新检索并运行。"
                run_metadata = {
                    **(report.run_metadata if run_metadata is None else run_metadata),
                    "current_stage": status,
                    "submission_validation": {
                        "policy": "report-input-v1",
                        "outcome": rejection or ("verified" if read_guard else "no_input"),
                        "request": expected_request,
                        "input_fingerprint": read_guard.get("fingerprint") if read_guard else None,
                    },
                }
            connection.execute(
                """
                UPDATE reports SET status = ?, content = ?, evidence_json = ?,
                    evaluation_json = ?, run_metadata_json = ?, error = ?,
                    updated_at = ? WHERE id = ?
                """,
                (
                    status,
                    report.content if content is None else content,
                    _dump(
                        [
                            item.model_dump()
                            for item in (report.evidence if evidence is None else evidence)
                        ]
                    ),
                    (
                        _dump(evaluation.model_dump())
                        if evaluation is not None
                        else (
                            _dump(report.evaluation.model_dump())
                            if report.evaluation is not None
                            else None
                        )
                    ),
                    _dump(report.run_metadata if run_metadata is None else run_metadata),
                    error,
                    now,
                    report_id,
                ),
            )
            job_error = (error or "报告生成失败")[:4000] if status == "failed" else None
            updated = connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = ?, lease_until = NULL, lease_owner = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (
                    status,
                    job_error,
                    now,
                    job_id,
                    expected_attempt,
                    expected_owner,
                    expected_owner,
                ),
            )
            if updated.rowcount != 1:
                raise StaleReportExecution(
                    f"Report {report_id} is no longer owned by attempt {expected_attempt}."
                )
        return self.get_report(report_id)

    def update_report(
        self,
        report_id: str,
        *,
        status: str,
        content: str | None = None,
        evidence: list[ReportEvidence] | None = None,
        evaluation: ReportEvaluation | None = None,
        run_metadata: dict[str, Any] | None = None,
        error: str | None = None,
        expected_job_attempt: int | None = None,
        expected_job_owner: str | None = None,
        expected_job_id: str | None = None,
    ) -> ResearchReport:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if row is None:
                raise KeyError(report_id)
            report = self._report_from_row(row)
            if expected_job_attempt is not None and report.status not in {"queued", "running"}:
                raise StaleReportExecution(f"Report {report_id} is already terminal.")
            where_clause = "WHERE id = ?"
            parameters: list[Any] = [
                status,
                report.content if content is None else content,
                _dump(
                    [
                        item.model_dump()
                        for item in (report.evidence if evidence is None else evidence)
                    ]
                ),
                (
                    _dump(evaluation.model_dump())
                    if evaluation is not None
                    else (
                        _dump(report.evaluation.model_dump())
                        if report.evaluation is not None
                        else None
                    )
                ),
                _dump(report.run_metadata if run_metadata is None else run_metadata),
                error,
                _now(),
                report_id,
            ]
            if expected_job_attempt is not None:
                where_clause += """
                    AND EXISTS (
                        SELECT 1 FROM knowledge_jobs AS job
                        WHERE job.kind = 'report' AND job.resource_id = reports.id
                          AND job.status = 'running' AND job.attempts = ?
                          AND (? IS NULL OR job.id = ?)
                          AND (? IS NULL OR job.lease_owner = ?)
                    )
                """
                parameters.extend(
                    (expected_job_attempt, expected_job_id, expected_job_id,
                     expected_job_owner, expected_job_owner)
                )
            updated = connection.execute(
                f"""
                UPDATE reports SET status = ?, content = ?, evidence_json = ?,
                    evaluation_json = ?, run_metadata_json = ?, error = ?,
                    updated_at = ? {where_clause}
                """,
                parameters,
            )
        if expected_job_attempt is not None and updated.rowcount != 1:
            raise StaleReportExecution(
                f"Report {report_id} is no longer owned by attempt {expected_job_attempt}."
            )
        return self.get_report(report_id)

    def _report_from_row(self, row: sqlite3.Row) -> ResearchReport:
        evaluation = (
            ReportEvaluation.model_validate_json(row["evaluation_json"])
            if row["evaluation_json"]
            else None
        )
        return ResearchReport(
            id=row["id"],
            query=row["query"],
            topic_slugs=json.loads(row["topic_slugs_json"]),
            top_k=row["top_k"],
            report_depth=row["report_depth"],
            status=row["status"],
            content=row["content"] or "",
            evidence=[
                ReportEvidence.model_validate(item)
                for item in json.loads(row["evidence_json"] or "[]")
            ],
            evaluation=evaluation,
            run_metadata=json.loads(row["run_metadata_json"] or "{}"),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
