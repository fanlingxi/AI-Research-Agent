from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.knowledge.schemas import (
    CandidateEntity,
    CandidateRelation,
    CandidateStatus,
    KnowledgeIngestion,
    KnowledgeJob,
    MergeSuggestion,
    ProjectionEvent,
    PublishedEntity,
    PublishedRelation,
    ReportEvaluation,
    ReportEvidence,
    ResearchReport,
)

CandidateKind = Literal["entity", "relation"]


class DecisionAlreadyApplied(ValueError):
    """Raised when a concurrent reviewer already moved a candidate out of draft."""

    def __init__(self, candidate_id: str) -> None:
        super().__init__(f"candidate {candidate_id} already has a terminal decision")
        self.candidate_id = candidate_id


class KnowledgeRepository:
    """SQLite source of truth for ingestion, review, jobs, reports, and outbox state."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
        self._apply_migration(1, _BASE_SCHEMA)
        self._apply_migration(2, _RELIABILITY_SCHEMA)
        self._apply_migration(3, _CONSISTENCY_SCHEMA)
        self._apply_migration(4, _REPORT_METADATA_SCHEMA)

    def _apply_migration(self, version: int, sql: str) -> None:
        with self._connect() as connection:
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if applied:
                return
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    # Ingestions and durable jobs -------------------------------------------------

    def create_ingestion(
        self,
        *,
        topic: str,
        sources: list[str],
        pdf_max_pages: int,
        enqueue: bool = True,
    ) -> KnowledgeIngestion:
        ingestion_id = f"ing-{uuid4().hex}"
        created_at = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO ingestions (
                    id, topic, topic_slug, sources_json, pdf_max_pages,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (
                    ingestion_id,
                    topic.strip(),
                    slugify(topic),
                    _dump(sources),
                    pdf_max_pages,
                    created_at,
                    created_at,
                ),
            )
            if enqueue:
                self._enqueue_job_tx(
                    connection,
                    kind="ingestion",
                    resource_id=ingestion_id,
                    payload={},
                )
        return self.get_ingestion(ingestion_id)

    def list_ingestions(self, limit: int = 100) -> list[KnowledgeIngestion]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ingestions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._ingestion_from_row(row) for row in rows]

    def get_ingestion(self, ingestion_id: str) -> KnowledgeIngestion:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ingestions WHERE id = ?", (ingestion_id,)
            ).fetchone()
        if row is None:
            raise KeyError(ingestion_id)
        return self._ingestion_from_row(row)

    def update_ingestion(
        self,
        ingestion_id: str,
        *,
        status: str,
        error: str | None = None,
        vault_path: str | None = None,
    ) -> KnowledgeIngestion:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ingestions
                SET status = ?, error = ?, vault_path = COALESCE(?, vault_path), updated_at = ?
                WHERE id = ?
                """,
                (status, error, vault_path, _now(), ingestion_id),
            )
        return self.get_ingestion(ingestion_id)

    def reset_ingestion(self, ingestion_id: str) -> KnowledgeIngestion:
        """Reset failed extraction work while preserving the ingestion identity."""
        projection_requeued = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            failed_projection = connection.execute(
                """
                SELECT COUNT(*) FROM projection_outbox
                WHERE ingestion_id = ? AND status = 'failed'
                """,
                (ingestion_id,),
            ).fetchone()[0]
            if failed_projection:
                connection.execute(
                    """
                    UPDATE projection_outbox
                    SET status = 'queued', lease_until = NULL, last_error = NULL, updated_at = ?
                    WHERE ingestion_id = ? AND status = 'failed'
                    """,
                    (_now(), ingestion_id),
                )
                connection.execute(
                    """
                    UPDATE ingestions
                    SET status = 'publishing', error = NULL, updated_at = ? WHERE id = ?
                    """,
                    (_now(), ingestion_id),
                )
                projection_requeued = True

            if not projection_requeued:
                connection.execute(
                    "DELETE FROM projection_outbox WHERE ingestion_id = ?", (ingestion_id,)
                )
                connection.execute(
                    "DELETE FROM review_events WHERE candidate_id IN "
                    "(SELECT id FROM candidates WHERE ingestion_id = ?)",
                    (ingestion_id,),
                )
                connection.execute("DELETE FROM candidates WHERE ingestion_id = ?", (ingestion_id,))
                connection.execute("DELETE FROM documents WHERE ingestion_id = ?", (ingestion_id,))
                connection.execute(
                    """
                    UPDATE ingestions SET status = 'queued', error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (_now(), ingestion_id),
                )
                self._enqueue_job_tx(
                    connection,
                    kind="ingestion",
                    resource_id=ingestion_id,
                    payload={},
                    force_requeue=True,
                )
        return self.get_ingestion(ingestion_id)

    def mark_interrupted(self) -> None:
        """Compatibility helper for explicit administrative interruption."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ingestions
                SET status = 'interrupted', updated_at = ?,
                    error = COALESCE(error, '任务执行被中断，可在任务历史中重试。')
                WHERE status = 'running'
                """,
                (_now(),),
            )

    def recover_running_work(self) -> None:
        """Return only expired work to the queue; active worker leases remain owned."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'queued', lease_until = NULL, updated_at = ?
                WHERE status = 'running' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'queued', lease_until = NULL, updated_at = ?
                WHERE status = 'running' AND (lease_until IS NULL OR lease_until < ?)
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE ingestions SET status = 'queued', updated_at = ?
                WHERE status = 'running' AND EXISTS (
                    SELECT 1 FROM knowledge_jobs job
                    WHERE job.kind = 'ingestion' AND job.resource_id = ingestions.id
                      AND job.status = 'queued'
                )
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE reports SET status = 'queued', updated_at = ?
                WHERE status = 'running' AND EXISTS (
                    SELECT 1 FROM knowledge_jobs job
                    WHERE job.kind = 'report' AND job.resource_id = reports.id
                      AND job.status = 'queued'
                )
                """,
                (now,),
            )

    def enqueue_job(
        self,
        *,
        kind: Literal["ingestion", "report"],
        resource_id: str,
        payload: dict[str, Any] | None = None,
        force_requeue: bool = False,
    ) -> KnowledgeJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job_id = self._enqueue_job_tx(
                connection,
                kind=kind,
                resource_id=resource_id,
                payload=payload or {},
                force_requeue=force_requeue,
            )
        return self.get_job(job_id)

    def _enqueue_job_tx(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        resource_id: str,
        payload: dict[str, Any],
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
                    SET status = 'queued', payload_json = ?, lease_until = NULL,
                        last_error = NULL, updated_at = ? WHERE id = ?
                    """,
                    (_dump(payload), _now(), existing["id"]),
                )
            return str(existing["id"])
        job_id = f"job-{uuid4().hex}"
        now = _now()
        connection.execute(
            """
            INSERT INTO knowledge_jobs (
                id, kind, resource_id, status, payload_json, attempts,
                lease_until, last_error, created_at, updated_at
            ) VALUES (?, ?, ?, 'queued', ?, 0, NULL, NULL, ?, ?)
            """,
            (job_id, kind, resource_id, _dump(payload), now, now),
        )
        return job_id

    def get_job(self, job_id: str) -> KnowledgeJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_from_row(row)

    def claim_job(self, lease_seconds: int = 120) -> KnowledgeJob | None:
        now = datetime.now(tz=UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE knowledge_jobs SET status = 'queued', lease_until = NULL, updated_at = ?
                WHERE status = 'running' AND lease_until < ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'running', attempts = attempts + 1, lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lease_until, now.isoformat(), row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._job_from_row(claimed)

    def complete_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'completed', lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_now(), job_id),
            )

    def complete_resource_job(self, kind: str, resource_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'completed', lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE kind = ? AND resource_id = ?
                """,
                (_now(), kind, resource_id),
            )

    def fail_job(self, job_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:4000], _now(), job_id),
            )

    def job_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM knowledge_jobs GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def projection_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM projection_outbox GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    # Documents and candidates ---------------------------------------------------

    def add_document(
        self,
        *,
        ingestion_id: str,
        document_id: str,
        title: str,
        source: str,
        source_url: str | None,
        local_path: str | None,
        pages: int,
        metadata: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO documents (
                    id, ingestion_id, title, source, source_url, local_path, pages, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    ingestion_id,
                    title,
                    source,
                    source_url,
                    local_path,
                    pages,
                    _dump(metadata),
                ),
            )

    def add_candidate_entity(self, candidate: CandidateEntity) -> None:
        self._add_candidate("entity", candidate)

    def add_candidate_relation(self, candidate: CandidateRelation) -> None:
        self._add_candidate("relation", candidate)

    def _add_candidate(
        self, kind: CandidateKind, candidate: CandidateEntity | CandidateRelation
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO candidates (
                    id, ingestion_id, kind, status, canonical_id, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.id,
                    candidate.ingestion_id,
                    kind,
                    candidate.status,
                    getattr(candidate, "canonical_id", None),
                    _dump(candidate.model_dump()),
                    now,
                    now,
                ),
            )

    def list_candidates(
        self, ingestion_id: str, status: CandidateStatus | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM candidates WHERE ingestion_id = ?"
        params: list[Any] = [ingestion_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY kind, created_at"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return self._candidate_from_row(row)

    def get_review_decision(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT decision, canonical_id, created_at FROM review_events
                WHERE candidate_id = ? ORDER BY created_at LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_candidate(self, candidate_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        stored = self.get_candidate(candidate_id)
        if stored["candidate"]["status"] != "draft":
            raise ValueError("only draft candidates can be edited")
        payload = dict(stored["candidate"])
        allowed = (
            {"name", "summary", "aliases", "confidence"}
            if stored["kind"] == "entity"
            else {"summary", "confidence"}
        )
        payload.update({key: value for key, value in changes.items() if key in allowed})
        candidate = self._model_for_kind(stored["kind"], payload)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE candidates SET payload_json = ?, updated_at = ?
                WHERE id = ? AND status = 'draft'
                """,
                (_dump(candidate.model_dump()), _now(), candidate_id),
            )
        return self.get_candidate(candidate_id)

    def set_merge_suggestions(
        self, candidate_id: str, suggestions: list[MergeSuggestion]
    ) -> dict[str, Any]:
        stored = self.get_candidate(candidate_id)
        if stored["kind"] != "entity":
            raise ValueError("only entity candidates can have merge suggestions")
        payload = dict(stored["candidate"])
        payload["merge_suggestions"] = [item.model_dump() for item in suggestions]
        candidate = CandidateEntity.model_validate(payload)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE candidates SET payload_json = ?, updated_at = ?
                WHERE id = ? AND status = 'draft'
                """,
                (_dump(candidate.model_dump()), _now(), candidate_id),
            )
        return self.get_candidate(candidate_id)

    # Transactional review and outbox -------------------------------------------

    def reject_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = self._candidate_from_row(row)
            payload = dict(stored["candidate"])
            payload["status"] = "rejected"
            self._record_decision_tx(
                connection,
                row=row,
                payload=payload,
                decision="reject",
                status="rejected",
                canonical_id=None,
            )
        return self.get_candidate(candidate_id)

    def record_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        status: CandidateStatus,
        canonical_id: str | None = None,
    ) -> dict[str, Any]:
        if decision == "reject" and status == "rejected":
            return self.reject_candidate(candidate_id)
        raise ValueError("publish entities and relations through their transactional methods")

    def publish_entity(self, candidate_id: str, canonical_id: str | None = None) -> PublishedEntity:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = self._candidate_from_row(row)
            if stored["kind"] != "entity":
                raise ValueError("only entity candidates can be published as entities")
            candidate = CandidateEntity.model_validate(stored["candidate"])
            target_id = canonical_id or candidate.canonical_id
            now = _now()
            if target_id:
                existing_row = connection.execute(
                    "SELECT payload_json FROM published_entities WHERE id = ?", (target_id,)
                ).fetchone()
                if existing_row is None:
                    raise ValueError("canonical entity not found")
                existing = PublishedEntity.model_validate_json(existing_row["payload_json"])
                entity = existing.model_copy(
                    update={
                        "aliases": _unique(existing.aliases + [candidate.name] + candidate.aliases),
                        "evidence": _unique_models(existing.evidence + [candidate.evidence]),
                        "topic_slugs": _unique(existing.topic_slugs + [candidate.topic_slug]),
                    }
                )
                connection.execute(
                    "UPDATE published_entities SET payload_json = ?, updated_at = ? WHERE id = ?",
                    (_dump(entity.model_dump()), now, entity.id),
                )
                status: CandidateStatus = "merged"
                decision = "merge"
            else:
                entity = PublishedEntity(
                    id=f"entity-{uuid4().hex}",
                    name=candidate.name,
                    type=candidate.type,
                    summary=candidate.summary,
                    aliases=_unique(candidate.aliases),
                    evidence=[candidate.evidence],
                    topic_slugs=[candidate.topic_slug],
                    metadata=candidate.metadata,
                )
                try:
                    connection.execute(
                        """
                        INSERT INTO published_entities (
                            id, normalized_name, entity_type, payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entity.id,
                            _normalize(entity.name),
                            entity.type,
                            _dump(entity.model_dump()),
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("检测到已有规范实体，请人工确认合并后再发布。") from exc
                status = "published"
                decision = "approve"
            connection.execute(
                "INSERT OR IGNORE INTO entity_topics (entity_id, topic_slug) VALUES (?, ?)",
                (entity.id, candidate.topic_slug),
            )
            payload = candidate.model_dump()
            payload.update({"status": status, "canonical_id": entity.id})
            self._record_decision_tx(
                connection,
                row=row,
                payload=payload,
                decision=decision,
                status=status,
                canonical_id=entity.id,
            )
            self._enqueue_projection_tx(
                connection,
                ingestion_id=candidate.ingestion_id,
                candidate_id=candidate.id,
                aggregate_type="entity",
                aggregate_id=entity.id,
                topic_slug=candidate.topic_slug,
            )
        return entity

    def publish_relation(self, candidate_id: str) -> PublishedRelation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = self._candidate_from_row(row)
            if stored["kind"] != "relation":
                raise ValueError("only relation candidates can be published as relations")
            candidate = CandidateRelation.model_validate(stored["candidate"])
            source_row = self._candidate_row_tx(connection, candidate.source_candidate_id)
            target_row = self._candidate_row_tx(connection, candidate.target_candidate_id)
            source = CandidateEntity.model_validate(
                self._candidate_from_row(source_row)["candidate"]
            )
            target = CandidateEntity.model_validate(
                self._candidate_from_row(target_row)["candidate"]
            )
            if not source.canonical_id or not target.canonical_id:
                raise ValueError("approve both endpoint entities before publishing a relation")
            relation = PublishedRelation(
                id=f"relation-{uuid4().hex}",
                source_entity_id=source.canonical_id,
                target_entity_id=target.canonical_id,
                type=candidate.type,
                summary=candidate.summary,
                confidence=candidate.confidence,
                evidence=[candidate.evidence],
                topic_slugs=[candidate.topic_slug],
                metadata=candidate.metadata,
            )
            now = _now()
            connection.execute(
                """
                INSERT INTO published_relations (
                    id, source_entity_id, target_entity_id, relation_type,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relation.id,
                    relation.source_entity_id,
                    relation.target_entity_id,
                    relation.type,
                    _dump(relation.model_dump()),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO relation_topics (relation_id, topic_slug) VALUES (?, ?)",
                (relation.id, candidate.topic_slug),
            )
            payload = candidate.model_dump()
            payload["status"] = "published"
            self._record_decision_tx(
                connection,
                row=row,
                payload=payload,
                decision="approve",
                status="published",
                canonical_id=None,
            )
            self._enqueue_projection_tx(
                connection,
                ingestion_id=candidate.ingestion_id,
                candidate_id=candidate.id,
                aggregate_type="relation",
                aggregate_id=relation.id,
                topic_slug=candidate.topic_slug,
            )
        return relation

    def _candidate_row_tx(self, connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return row

    def _record_decision_tx(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        payload: dict[str, Any],
        decision: str,
        status: CandidateStatus,
        canonical_id: str | None,
    ) -> None:
        candidate = self._model_for_kind(row["kind"], payload)
        updated = connection.execute(
            """
            UPDATE candidates
            SET status = ?, canonical_id = ?, payload_json = ?, updated_at = ?
            WHERE id = ? AND status = 'draft'
            """,
            (status, canonical_id, _dump(candidate.model_dump()), _now(), row["id"]),
        )
        if updated.rowcount != 1:
            raise DecisionAlreadyApplied(str(row["id"]))
        connection.execute(
            """
            INSERT INTO review_events (
                id, candidate_id, decision, canonical_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"review-{uuid4().hex}",
                row["id"],
                decision,
                canonical_id,
                _dump(payload),
                _now(),
            ),
        )

    def _enqueue_projection_tx(
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

    def projection_status_for_candidate(self, candidate_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM projection_outbox WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
        return str(row["status"]) if row else "not_required"

    def claim_projection(self, lease_seconds: int = 120) -> ProjectionEvent | None:
        now = datetime.now(tz=UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE projection_outbox SET status = 'queued', lease_until = NULL, updated_at = ?
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
                SET status = 'running', attempts = attempts + 1, lease_until = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lease_until, now.isoformat(), row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM projection_outbox WHERE id = ?", (row["id"],)
            ).fetchone()
        return self._projection_from_row(claimed)

    def complete_projection(self, event_id: str) -> KnowledgeIngestion:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ingestion_id FROM projection_outbox WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'completed', lease_until = NULL, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_now(), event_id),
            )
            ingestion_id = str(row["ingestion_id"])
        return self.refresh_ingestion_status(ingestion_id)

    def fail_projection(self, event_id: str, error: str) -> KnowledgeIngestion:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT ingestion_id FROM projection_outbox WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            ingestion_id = str(row["ingestion_id"])
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'failed', lease_until = NULL, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error[:4000], _now(), event_id),
            )
            connection.execute(
                """
                UPDATE ingestions SET status = 'failed', error = ?, updated_at = ? WHERE id = ?
                """,
                (f"正式投影失败：{error}"[:4000], _now(), ingestion_id),
            )
        return self.get_ingestion(ingestion_id)

    # Published knowledge --------------------------------------------------------

    def find_merge_suggestions(
        self, *, name: str, entity_type: str, aliases: list[str]
    ) -> list[MergeSuggestion]:
        normalized = {_normalize(name), *(_normalize(alias) for alias in aliases)}
        matches: list[MergeSuggestion] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM published_entities WHERE entity_type = ?",
                (entity_type,),
            ).fetchall()
        for row in rows:
            entity = PublishedEntity.model_validate_json(row["payload_json"])
            known = {_normalize(entity.name), *(_normalize(alias) for alias in entity.aliases)}
            if normalized.intersection(known):
                matches.append(
                    MergeSuggestion(
                        entity_id=entity.id,
                        name=entity.name,
                        type=entity.type,
                        similarity=1.0,
                        match_kind="exact",
                    )
                )
                continue
            similarity = max(
                (
                    SequenceMatcher(a=left, b=right).ratio()
                    for left in normalized
                    for right in known
                ),
                default=0.0,
            )
            if similarity >= 0.82:
                matches.append(
                    MergeSuggestion(
                        entity_id=entity.id,
                        name=entity.name,
                        type=entity.type,
                        similarity=round(similarity, 3),
                        match_kind="similar",
                    )
                )
        return sorted(matches, key=lambda item: (-item.similarity, item.name.casefold()))

    def get_published_entity(self, entity_id: str) -> PublishedEntity:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM published_entities WHERE id = ?", (entity_id,)
            ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        return PublishedEntity.model_validate_json(row["payload_json"])

    def get_published_relation(self, relation_id: str) -> PublishedRelation:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM published_relations WHERE id = ?", (relation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(relation_id)
        return PublishedRelation.model_validate_json(row["payload_json"])

    def list_published_entities(self, topic_slug: str | None = None) -> list[PublishedEntity]:
        query = "SELECT payload_json FROM published_entities"
        params: tuple[Any, ...] = ()
        if topic_slug:
            query = """
                SELECT e.payload_json FROM published_entities e
                JOIN entity_topics t ON t.entity_id = e.id WHERE t.topic_slug = ?
                """
            params = (topic_slug,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [PublishedEntity.model_validate_json(row["payload_json"]) for row in rows]

    def list_published_relations(self, topic_slug: str | None = None) -> list[PublishedRelation]:
        query = "SELECT payload_json FROM published_relations"
        params: tuple[Any, ...] = ()
        if topic_slug:
            query = """
                SELECT r.payload_json FROM published_relations r
                JOIN relation_topics t ON t.relation_id = r.id WHERE t.topic_slug = ?
                """
            params = (topic_slug,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [PublishedRelation.model_validate_json(row["payload_json"]) for row in rows]

    def list_topics(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT topic_slug, MAX(topic) AS topic, MAX(updated_at) AS updated_at,
                       MAX(vault_path) AS vault_path
                FROM ingestions GROUP BY topic_slug ORDER BY updated_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def published_paper_ids(self, topic_slugs: list[str] | None = None) -> set[str]:
        entities: list[PublishedEntity] = []
        if topic_slugs:
            for slug in topic_slugs:
                entities.extend(self.list_published_entities(slug))
        else:
            entities = self.list_published_entities()
        return {
            evidence.paper_id
            for entity in entities
            for evidence in entity.evidence
            if evidence.paper_id
        }

    def refresh_ingestion_status(self, ingestion_id: str) -> KnowledgeIngestion:
        with self._connect() as connection:
            draft_count = connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE ingestion_id = ? AND status IN ('draft', 'approved')
                """,
                (ingestion_id,),
            ).fetchone()[0]
            pending = connection.execute(
                """
                SELECT COUNT(*) FROM projection_outbox
                WHERE ingestion_id = ? AND status IN ('queued', 'running')
                """,
                (ingestion_id,),
            ).fetchone()[0]
            failed = connection.execute(
                """
                SELECT COUNT(*) FROM projection_outbox
                WHERE ingestion_id = ? AND status = 'failed'
                """,
                (ingestion_id,),
            ).fetchone()[0]
        ingestion = self.get_ingestion(ingestion_id)
        if ingestion.status in {"queued", "running"}:
            return ingestion
        if draft_count:
            status = "needs_review"
        elif failed:
            status = "failed"
        elif pending:
            status = "publishing"
        else:
            status = "completed"
        return self.update_ingestion(ingestion_id, status=status, error=ingestion.error)

    # Reports --------------------------------------------------------------------

    def create_report(
        self,
        *,
        query: str,
        topic_slugs: list[str],
        top_k: int,
        report_depth: str,
        run_metadata: dict[str, Any] | None = None,
    ) -> ResearchReport:
        report_id = f"report-{uuid4().hex}"
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
                    report_id,
                    query.strip(),
                    _dump(topic_slugs),
                    top_k,
                    report_depth,
                    _dump(run_metadata or {}),
                    now,
                    now,
                ),
            )
            self._enqueue_job_tx(
                connection,
                kind="report",
                resource_id=report_id,
                payload={},
            )
        return self.get_report(report_id)

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
    ) -> ResearchReport:
        report = self.get_report(report_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE reports SET status = ?, content = ?, evidence_json = ?,
                    evaluation_json = ?, run_metadata_json = ?, error = ?,
                    updated_at = ? WHERE id = ?
                """,
                (
                    status,
                    report.content if content is None else content,
                    _dump([item.model_dump() for item in (evidence or report.evidence)]),
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
                ),
            )
        return self.get_report(report_id)

    # Row conversion -------------------------------------------------------------

    def _ingestion_from_row(self, row: sqlite3.Row) -> KnowledgeIngestion:
        with self._connect() as connection:
            document_count = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE ingestion_id = ?", (row["id"],)
            ).fetchone()[0]
            candidate_count = connection.execute(
                "SELECT COUNT(*) FROM candidates WHERE ingestion_id = ?", (row["id"],)
            ).fetchone()[0]
            published_count = connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE ingestion_id = ? AND status IN ('published', 'merged')
                """,
                (row["id"],),
            ).fetchone()[0]
            queued_job_count = connection.execute(
                """
                SELECT COUNT(*) FROM knowledge_jobs
                WHERE kind = 'ingestion' AND resource_id = ? AND status IN ('queued', 'running')
                """,
                (row["id"],),
            ).fetchone()[0]
            pending_projection_count = connection.execute(
                """
                SELECT COUNT(*) FROM projection_outbox
                WHERE ingestion_id = ? AND status IN ('queued', 'running')
                """,
                (row["id"],),
            ).fetchone()[0]
            failed_projection_count = connection.execute(
                """
                SELECT COUNT(*) FROM projection_outbox
                WHERE ingestion_id = ? AND status = 'failed'
                """,
                (row["id"],),
            ).fetchone()[0]
        return KnowledgeIngestion(
            id=row["id"],
            topic=row["topic"],
            topic_slug=row["topic_slug"],
            sources=json.loads(row["sources_json"]),
            pdf_max_pages=row["pdf_max_pages"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            vault_path=row["vault_path"],
            document_count=document_count,
            candidate_count=candidate_count,
            published_count=published_count,
            queued_job_count=queued_job_count,
            pending_projection_count=pending_projection_count,
            failed_projection_count=failed_projection_count,
        )

    def _candidate_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        payload["status"] = row["status"]
        payload["canonical_id"] = row["canonical_id"]
        model = self._model_for_kind(row["kind"], payload)
        return {"kind": row["kind"], "candidate": model.model_dump()}

    def _model_for_kind(
        self, kind: str, payload: dict[str, Any]
    ) -> CandidateEntity | CandidateRelation:
        if kind == "entity":
            return CandidateEntity.model_validate(payload)
        payload.pop("canonical_id", None)
        return CandidateRelation.model_validate(payload)

    def _job_from_row(self, row: sqlite3.Row) -> KnowledgeJob:
        return KnowledgeJob(
            id=row["id"],
            kind=row["kind"],
            resource_id=row["resource_id"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            attempts=row["attempts"],
            lease_until=row["lease_until"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _projection_from_row(self, row: sqlite3.Row) -> ProjectionEvent:
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
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

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


def slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.strip().lower())
    return normalized.strip("-")[:80] or "untitled-topic"


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().casefold())


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _unique_models(values: list[Any]) -> list[Any]:
    deduped: dict[str, Any] = {}
    for value in values:
        deduped.setdefault(value.model_dump_json(), value)
    return list(deduped.values())


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingestions (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    topic_slug TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    pdf_max_pages INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    vault_path TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    ingestion_id TEXT NOT NULL REFERENCES ingestions(id),
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    local_path TEXT,
    pages INTEGER NOT NULL,
    metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY,
    ingestion_id TEXT NOT NULL REFERENCES ingestions(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    canonical_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidates_ingestion_idx ON candidates(ingestion_id, status, kind);
CREATE TABLE IF NOT EXISTS review_events (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    decision TEXT NOT NULL,
    canonical_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS published_entities (
    id TEXT PRIMARY KEY,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS published_entities_name_idx
    ON published_entities(normalized_name, entity_type);
CREATE TABLE IF NOT EXISTS published_relations (
    id TEXT PRIMARY KEY,
    source_entity_id TEXT NOT NULL REFERENCES published_entities(id),
    target_entity_id TEXT NOT NULL REFERENCES published_entities(id),
    relation_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_topics (
    entity_id TEXT NOT NULL REFERENCES published_entities(id),
    topic_slug TEXT NOT NULL,
    PRIMARY KEY(entity_id, topic_slug)
);
CREATE TABLE IF NOT EXISTS relation_topics (
    relation_id TEXT NOT NULL REFERENCES published_relations(id),
    topic_slug TEXT NOT NULL,
    PRIMARY KEY(relation_id, topic_slug)
);
"""


_RELIABILITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(kind, resource_id)
);
CREATE INDEX IF NOT EXISTS knowledge_jobs_status_idx ON knowledge_jobs(status, created_at);
CREATE TABLE IF NOT EXISTS projection_outbox (
    id TEXT PRIMARY KEY,
    ingestion_id TEXT NOT NULL REFERENCES ingestions(id),
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    topic_slug TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projection_status_idx ON projection_outbox(status, created_at);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    topic_slugs_json TEXT NOT NULL,
    top_k INTEGER NOT NULL,
    report_depth TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    evaluation_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


_CONSISTENCY_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS review_events_candidate_unique
    ON review_events(candidate_id);
CREATE INDEX IF NOT EXISTS knowledge_jobs_lease_idx
    ON knowledge_jobs(status, lease_until);
CREATE INDEX IF NOT EXISTS projection_outbox_lease_idx
    ON projection_outbox(status, lease_until);
"""


_REPORT_METADATA_SCHEMA = """
ALTER TABLE reports ADD COLUMN run_metadata_json TEXT NOT NULL DEFAULT '{}';
"""
