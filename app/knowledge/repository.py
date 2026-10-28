from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from app.knowledge.candidate_repository import (
    CandidateKind,
    CandidateRepository,
    _normalize,
    _unique,
    candidate_from_row,
    refresh_aggregate_collections_tx,
    upsert_source_mention_tx,
)
from app.knowledge.candidate_repository import (
    DecisionAlreadyApplied as DecisionAlreadyApplied,
)
from app.knowledge.candidate_repository import (
    StaleIngestionExecution as StaleIngestionExecution,
)
from app.knowledge.core_models import CoreEntityClaimOverride
from app.knowledge.core_repository import KnowledgeCoreRepository
from app.knowledge.projection_repository import (
    ProjectionRepository,
    refresh_ingestion_status_tx,
)
from app.knowledge.report_repository import ReportRepository
from app.knowledge.schemas import (
    CandidateEntity,
    CandidateRelation,
    CandidateStatus,
    ConceptSense,
    EvidenceSpan,
    KnowledgeCollection,
    KnowledgeIngestion,
    KnowledgeJob,
    MergeSuggestion,
    ProjectionEvent,
    PublishedEntity,
    PublishedRelation,
    SourceMention,
)
from app.memory.repository import MemoryRepository
from app.persistence.migrations import apply_structural_migration, sql_migration
from app.persistence.sqlite import SQLiteDatabase
from app.runtime.job_repository import JobRepository
from app.schemas.documents import DocumentChunk

INBOX_COLLECTION_NAME = "收件箱"
INBOX_COLLECTION_SLUG = "inbox"
ACTIVE_INGESTION_STATUSES = ("queued", "running", "needs_review", "publishing")


class KnowledgeRepository:
    """Knowledge ingestion, review and outbox persistence on the shared SQLite database.

    Composes the queue, report, candidate review, projection outbox, Core and
    memory repositories; cross-resource recovery keeps expired business state
    aligned with durable jobs.

    During Phase 1A, legacy ``published_*`` records remain the compatibility
    runtime source.  The v8+ Core is the migration target and new-write
    foundation; it is synchronized in the same transaction and is not a third
    independent fact store.
    """

    def __init__(self, path: str, *, max_crash_recoveries: int | None = None) -> None:
        self.path = path
        self.database = SQLiteDatabase(path)
        self._initialize()
        self.jobs = JobRepository(self.database, max_crash_recoveries=max_crash_recoveries)
        self.core_repository = KnowledgeCoreRepository(path, self.database)
        self.projections = ProjectionRepository(self.database)
        self.candidates = CandidateRepository(
            self.database, self.core_repository, self.projections
        )
        self.reports = ReportRepository(self.database, self.jobs, self.core_repository)
        self.memory_repository = MemoryRepository(path, self.database)

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

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
        self._apply_migration(5, _COLLECTION_SCHEMA)
        self._apply_migration(6, _SEMANTIC_LAYER_SCHEMA)
        self._apply_migration(7, _COLLECTION_RELATION_BACKFILL_SCHEMA)
        self._apply_structural_migration(8)
        self._apply_structural_migration(10)
        self._apply_structural_migration(11)
        self._apply_structural_migration(12)
        self._apply_structural_migration(13)
        self._apply_structural_migration(14)
        self._apply_structural_migration(15)
        self._apply_structural_migration(16)
        self._apply_structural_migration(17)
        self._apply_structural_migration(18)
        self._apply_structural_migration(19)
        self._apply_structural_migration(20)
        self._apply_structural_migration(21)
        self._apply_structural_migration(22)
        self._backfill_legacy_relation_collections()
        self._backfill_semantic_records()

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

    def _apply_structural_migration(self, version: int) -> None:
        with self._connect() as connection:
            apply_structural_migration(
                connection,
                version=version,
                sql=sql_migration(version),
                applied_at=_now(),
            )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def _backfill_legacy_relation_collections(self) -> None:
        """Synchronize payload collection metadata after the legacy relation migration."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.id FROM published_relations r
                WHERE EXISTS (
                    SELECT 1 FROM collection_memberships m
                    WHERE m.aggregate_type = 'relation' AND m.aggregate_id = r.id
                )
                AND COALESCE(json_array_length(r.payload_json, '$.collection_slugs'), 0) = 0
                """
            ).fetchall()
            now = _now()
            for row in rows:
                refresh_aggregate_collections_tx(connection, "relation", str(row["id"]), now)

    # Ingestions and durable jobs -------------------------------------------------

    def create_ingestion(
        self,
        *,
        topic: str | None = None,
        collection: str | None = None,
        sources: list[str],
        pdf_max_pages: int,
        enqueue: bool = True,
        auto_execute: bool = False,
    ) -> KnowledgeIngestion:
        ingestion, _ = self.create_or_reuse_active_ingestion(
            topic=topic,
            collection=collection,
            sources=sources,
            pdf_max_pages=pdf_max_pages,
            enqueue=enqueue,
            auto_execute=auto_execute,
        )
        return ingestion

    def create_or_reuse_active_ingestion(
        self,
        *,
        topic: str | None = None,
        collection: str | None = None,
        sources: list[str],
        pdf_max_pages: int,
        enqueue: bool = True,
        auto_execute: bool = False,
        reparse_payload: dict[str, Any] | None = None,
    ) -> tuple[KnowledgeIngestion, bool]:
        """Create one active ingestion per equivalent submission.

        ``BEGIN IMMEDIATE`` serializes concurrent browser submissions.  That makes the
        read-for-duplicate and the insert one atomic decision without relying on a
        best-effort UI guard.
        """
        collection_name = (
            collection or topic or INBOX_COLLECTION_NAME
        ).strip() or INBOX_COLLECTION_NAME
        collection_slug = (
            INBOX_COLLECTION_SLUG
            if collection_name == INBOX_COLLECTION_NAME
            else slugify(collection_name)
        )
        normalized_sources = _normalized_sources(sources)
        ingestion_id = f"ing-{uuid4().hex}"
        created_at = _now()
        existing_id: str | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if reparse_payload is not None:
                current = connection.execute(
                    """SELECT i.topic_slug, d.content_sha256 FROM documents d
                       JOIN ingestions i ON i.id = d.ingestion_id WHERE d.id = ?""",
                    (reparse_payload["document_id"],),
                ).fetchone()
                if (current is None or current["topic_slug"] != collection_slug
                        or current["content_sha256"] != reparse_payload["original_sha256"]):
                    raise ValueError("解析期间文献或所属集合已变化，请重新解析。")
                existing = connection.execute(
                    """SELECT m.ingestion_id FROM documents d
                       JOIN sources s ON s.id = d.source_id
                       JOIN ingestion_documents m ON m.document_id = d.id
                       JOIN ingestions i ON i.id = m.ingestion_id
                       WHERE s.canonical_uri = ? AND d.content_sha256 = ? AND i.topic_slug = ?
                         AND (? IS NULL OR s.version = ?)
                       ORDER BY i.created_at LIMIT 1""",
                    (reparse_payload["identity_uri"], reparse_payload["content_sha256"],
                     collection_slug,
                     reparse_payload.get("source_version")
                     if reparse_payload["parser_version"] in {"pypdf-v3", "pypdf-v4"} else None,
                     reparse_payload.get("source_version")),
                ).fetchone()
                if existing:
                    existing_id = str(existing[0])
            rows = connection.execute(
                """
                SELECT id, sources_json, reparse_payload_json FROM ingestions
                WHERE topic_slug = ? AND pdf_max_pages = ?
                  AND (status IN ('queued', 'running', 'needs_review', 'publishing')
                       OR reparse_payload_json IS NOT NULL)
                ORDER BY created_at
                """,
                (collection_slug, pdf_max_pages),
            ).fetchall()
            for row in rows:
                if existing_id is not None:
                    break
                previous = json.loads(row["reparse_payload_json"] or "null")
                if reparse_payload is not None:
                    if previous and all(
                        previous[key] == reparse_payload[key]
                        for key in ("identity_uri", "content_sha256", "parser_version")
                    ):
                        existing_id = str(row["id"])
                        break
                    continue
                if previous is not None:
                    continue
                if _normalized_sources(json.loads(row["sources_json"])) == normalized_sources:
                    existing_id = str(row["id"])
                    break
            if existing_id is None:
                connection.execute(
                    """
                    INSERT INTO ingestions (
                        id, topic, topic_slug, sources_json, pdf_max_pages,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        ingestion_id,
                        collection_name,
                        collection_slug,
                        _dump(normalized_sources),
                        pdf_max_pages,
                        created_at,
                        created_at,
                    ),
                )
                self._ensure_collection_tx(connection, collection_name, collection_slug)
                if reparse_payload is not None:
                    connection.execute(
                        "UPDATE ingestions SET reparse_payload_json = ? WHERE id = ?",
                        (_dump(reparse_payload), ingestion_id),
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO ingestion_collections (ingestion_id, collection_slug)
                    VALUES (?, ?)
                    """,
                    (ingestion_id, collection_slug),
                )
                if enqueue:
                    self.jobs.enqueue_job_tx(
                        connection,
                        kind="ingestion",
                        resource_id=ingestion_id,
                        payload={"auto_execute": True} if auto_execute else {},
                        priority=100 if auto_execute else 0,
                    )
            elif enqueue and auto_execute:
                job = connection.execute(
                    """
                    SELECT id, payload_json FROM knowledge_jobs
                    WHERE kind = 'ingestion' AND resource_id = ?
                    """,
                    (existing_id,),
                ).fetchone()
                if job is not None:
                    payload = json.loads(job["payload_json"] or "{}")
                    payload["auto_execute"] = True
                    connection.execute(
                        """
                        UPDATE knowledge_jobs
                        SET payload_json = ?, priority = MAX(priority, 100), updated_at = ?
                        WHERE id = ? AND status IN ('queued', 'running')
                        """,
                        (_dump(payload), _now(), job["id"]),
                    )
        if existing_id is not None:
            return self.get_ingestion(existing_id), True
        return self.get_ingestion(ingestion_id), False

    def reparse_payload(self, ingestion_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT reparse_payload_json FROM ingestions WHERE id = ?", (ingestion_id,)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

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

    def require_evidence_for_ingestion(
        self, ingestion_id: str, evidence: EvidenceSpan
    ) -> None:
        """Require a candidate EvidenceSpan to belong to its declared ingestion.

        Candidate publication already verifies the Source → Document → Chunk
        chain.  Domain authoring also needs to prevent a caller from attaching
        an otherwise valid EvidenceSpan from a different Collection ingestion.
        This keeps the existing ingestion/Collection ownership model intact.
        """

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM ingestion_documents AS membership
                JOIN documents AS d ON d.id = membership.document_id
                JOIN chunks AS c ON c.document_id = d.id
                WHERE membership.ingestion_id = ? AND d.id = ? AND c.id = ?
                """,
                (ingestion_id, evidence.paper_id, evidence.chunk_id),
            ).fetchone()
        if row is None:
            raise ValueError("Evidence must belong to the declared Knowledge ingestion.")

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

    def reset_ingestion(
        self, ingestion_id: str, *, auto_execute: bool = False
    ) -> KnowledgeIngestion:
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
                linked_document_ids = [
                    str(row["document_id"])
                    for row in connection.execute(
                        "SELECT document_id FROM ingestion_documents WHERE ingestion_id = ?",
                        (ingestion_id,),
                    ).fetchall()
                ]
                evidence_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM evidences evidence
                    JOIN chunks chunk ON chunk.id = evidence.chunk_id
                    JOIN ingestion_documents membership
                      ON membership.document_id = chunk.document_id
                    WHERE membership.ingestion_id = ?
                    """,
                    (ingestion_id,),
                ).fetchone()[0]
                if evidence_count:
                    raise ValueError(
                        "cannot reset an ingestion whose chunks support published Core evidence"
                    )
                connection.execute(
                    "DELETE FROM ingestion_documents WHERE ingestion_id = ?",
                    (ingestion_id,),
                )
                for document_id in linked_document_ids:
                    still_linked = connection.execute(
                        "SELECT 1 FROM ingestion_documents WHERE document_id = ? LIMIT 1",
                        (document_id,),
                    ).fetchone()
                    if still_linked is not None:
                        continue
                    connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
                    connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))
                connection.execute(
                    """
                    UPDATE ingestions SET status = 'queued', error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (_now(), ingestion_id),
                )
                self.jobs.enqueue_job_tx(
                    connection,
                    kind="ingestion",
                    resource_id=ingestion_id,
                    payload={"auto_execute": True} if auto_execute else {},
                    priority=100 if auto_execute else 0,
                    force_requeue=True,
                )
        return self.get_ingestion(ingestion_id)

    # Collections ---------------------------------------------------------------

    def _ensure_collection_tx(
        self, connection: sqlite3.Connection, name: str, slug: str | None = None
    ) -> KnowledgeCollection:
        collection_slug = slug or slugify(name)
        is_system = int(collection_slug == INBOX_COLLECTION_SLUG)
        now = _now()
        connection.execute(
            """
            INSERT INTO knowledge_collections (slug, name, is_system, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            """,
            (collection_slug, name.strip() or INBOX_COLLECTION_NAME, is_system, now, now),
        )
        return KnowledgeCollection(slug=collection_slug, name=name, is_system=bool(is_system))

    def create_collection(self, name: str) -> KnowledgeCollection:
        normalized = name.strip()
        if len(normalized) < 2:
            raise ValueError("知识集合名称至少需要 2 个字符。")
        with self._connect() as connection:
            collection = self._ensure_collection_tx(
                connection,
                normalized,
                INBOX_COLLECTION_SLUG if normalized == INBOX_COLLECTION_NAME else None,
            )
        return self.get_collection(collection.slug)

    def get_collection(self, collection_slug: str) -> KnowledgeCollection:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT c.slug, c.name, c.is_system, c.updated_at,
                       COUNT(ic.ingestion_id) AS ingestion_count
                FROM knowledge_collections c
                LEFT JOIN ingestion_collections ic ON ic.collection_slug = c.slug
                WHERE c.slug = ? GROUP BY c.slug
                """,
                (collection_slug,),
            ).fetchone()
        if row is None:
            raise KeyError(collection_slug)
        return KnowledgeCollection(**dict(row))

    def list_collections(self) -> list[KnowledgeCollection]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.slug, c.name, c.is_system, c.updated_at,
                       COUNT(ic.ingestion_id) AS ingestion_count
                FROM knowledge_collections c
                LEFT JOIN ingestion_collections ic ON ic.collection_slug = c.slug
                GROUP BY c.slug
                ORDER BY c.is_system DESC, c.updated_at DESC, c.name COLLATE NOCASE
                """
            ).fetchall()
        return [KnowledgeCollection(**dict(row)) for row in rows]

    def move_ingestion_collection(self, ingestion_id: str, collection: str) -> KnowledgeIngestion:
        new_name = collection.strip() or INBOX_COLLECTION_NAME
        new_slug = INBOX_COLLECTION_SLUG if new_name == INBOX_COLLECTION_NAME else slugify(new_name)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ingestion = connection.execute(
                "SELECT status, topic_slug FROM ingestions WHERE id = ?", (ingestion_id,)
            ).fetchone()
            if ingestion is None:
                raise KeyError(ingestion_id)
            if ingestion["status"] in {"queued", "running", "publishing"}:
                raise ValueError("任务排队、运行或投影期间不能移动知识集合。")
            old_slug = str(ingestion["topic_slug"])
            if old_slug == new_slug:
                affected_slugs = [new_slug]
            else:
                self._ensure_collection_tx(connection, new_name, new_slug)
                now = _now()
                connection.execute(
                    "UPDATE ingestions SET topic = ?, topic_slug = ?, updated_at = ? WHERE id = ?",
                    (new_name, new_slug, now, ingestion_id),
                )
                connection.execute(
                    "UPDATE ingestion_collections SET collection_slug = ? WHERE ingestion_id = ?",
                    (new_slug, ingestion_id),
                )
                candidates = connection.execute(
                    "SELECT id, payload_json FROM candidates WHERE ingestion_id = ?",
                    (ingestion_id,),
                ).fetchall()
                for candidate in candidates:
                    payload = json.loads(candidate["payload_json"])
                    payload["topic_slug"] = new_slug
                    connection.execute(
                        "UPDATE candidates SET payload_json = ?, updated_at = ? WHERE id = ?",
                        (_dump(payload), now, candidate["id"]),
                    )
                memberships = connection.execute(
                    """
                    SELECT DISTINCT aggregate_type, aggregate_id FROM collection_memberships
                    WHERE ingestion_id = ?
                    """,
                    (ingestion_id,),
                ).fetchall()
                connection.execute(
                    "UPDATE collection_memberships SET collection_slug = ? WHERE ingestion_id = ?",
                    (new_slug, ingestion_id),
                )
                for item in memberships:
                    refresh_aggregate_collections_tx(
                        connection, str(item["aggregate_type"]), str(item["aggregate_id"]), now
                    )
                affected_slugs = [old_slug, new_slug]
            self.jobs.enqueue_job_tx(
                connection,
                kind="collection_sync",
                resource_id=ingestion_id,
                payload={"collection_slugs": affected_slugs},
                priority=20,
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
            self.jobs.recover_expired_jobs_tx(connection, now)
            connection.execute(
                """
                UPDATE projection_outbox
                SET status = 'queued', lease_until = NULL, lease_owner = NULL, updated_at = ?
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

    def mark_ingestion_for_dispatch(self, ingestion_id: str) -> KnowledgeIngestion:
        """Persist browser intent for the independent durable Worker."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ingestion = connection.execute(
                "SELECT status FROM ingestions WHERE id = ?", (ingestion_id,)
            ).fetchone()
            if ingestion is None:
                raise KeyError(ingestion_id)
            if ingestion["status"] not in {"queued", "running"}:
                raise ValueError(
                    f"Ingestion cannot be dispatched from {ingestion['status']}."
                )
            job = connection.execute(
                """
                SELECT id, status, payload_json FROM knowledge_jobs
                WHERE kind = 'ingestion' AND resource_id = ?
                """,
                (ingestion_id,),
            ).fetchone()
            if job is None:
                raise KeyError(ingestion_id)
            if job["status"] not in {"queued", "running"}:
                raise ValueError(f"Ingestion job cannot be dispatched from {job['status']}.")
            payload = json.loads(job["payload_json"] or "{}")
            payload["auto_execute"] = True
            connection.execute(
                """
                UPDATE knowledge_jobs
                SET payload_json = ?, priority = MAX(priority, 100), updated_at = ?
                WHERE id = ?
                """,
                (_dump(payload), _now(), job["id"]),
            )
        return self.get_ingestion(ingestion_id)

    def claim_dispatched_ingestion_job(
        self,
        lease_seconds: int = 120,
        *,
        owner_id: str | None = None,
    ) -> KnowledgeJob | None:
        """Claim the next ingestion explicitly marked for built-in durable dispatch."""
        now = datetime.now(tz=UTC)
        now_text = now.isoformat()
        owner = owner_id or f"executor-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT job.* FROM knowledge_jobs AS job
                JOIN ingestions AS ingestion ON ingestion.id = job.resource_id
                WHERE job.kind = 'ingestion'
                  AND job.status IN ('queued', 'running')
                  AND ingestion.status IN ('queued', 'running')
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
                    self.jobs.recover_expired_jobs_tx(connection, now_text, job_id=row["id"])
                    recovered = connection.execute(
                        "SELECT status FROM knowledge_jobs WHERE id = ?", (row["id"],)
                    ).fetchone()
                    if recovered["status"] != "queued":
                        continue
                    connection.execute(
                        """
                        UPDATE ingestions SET status = 'queued', updated_at = ?
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
            connection.execute(
                """
                UPDATE ingestions SET status = 'running', error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now_text, selected["resource_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ?", (selected["id"],)
            ).fetchone()
        return self.jobs.job_from_row(claimed)

    def begin_ingestion_execution(
        self,
        ingestion_id: str,
        job_id: str,
        *,
        expected_attempt: int,
        expected_owner: str | None = None,
    ) -> KnowledgeIngestion:
        """Mark one claimed ingestion running only while this attempt owns its job."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                """
                SELECT 1 FROM knowledge_jobs
                WHERE id = ? AND kind = 'ingestion' AND resource_id = ?
                  AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (job_id, ingestion_id, expected_attempt, expected_owner, expected_owner),
            ).fetchone()
            if owned is None:
                raise StaleIngestionExecution(
                    f"Ingestion {ingestion_id} is no longer owned by attempt {expected_attempt}."
                )
            connection.execute(
                """
                UPDATE ingestions SET status = 'running', error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_now(), ingestion_id),
            )
        return self.get_ingestion(ingestion_id)

    def assert_ingestion_execution(
        self,
        ingestion_id: str,
        job_id: str,
        *,
        expected_attempt: int,
        expected_owner: str | None = None,
    ) -> None:
        with self._connect() as connection:
            owned = connection.execute(
                """
                SELECT 1 FROM knowledge_jobs
                WHERE id = ? AND kind = 'ingestion' AND resource_id = ?
                  AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (job_id, ingestion_id, expected_attempt, expected_owner, expected_owner),
            ).fetchone()
        if owned is None:
            raise StaleIngestionExecution(
                f"Ingestion {ingestion_id} is no longer owned by attempt {expected_attempt}."
            )

    def projection_summary(self) -> dict[str, int]:
        return self.projections.projection_summary()

    # Documents and candidates (CandidateRepository) ------------------------------

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
        self.candidates.add_document(
            ingestion_id=ingestion_id,
            document_id=document_id,
            title=title,
            source=source,
            source_url=source_url,
            local_path=local_path,
            pages=pages,
            metadata=metadata,
        )

    def add_candidate_entity(self, candidate: CandidateEntity) -> None:
        self.candidates.add_candidate_entity(candidate)

    def add_candidate_relation(self, candidate: CandidateRelation) -> None:
        self.candidates.add_candidate_relation(candidate)

    def replace_draft_candidates_for_paper(
        self,
        ingestion_id: str,
        paper_id: str,
        candidates: list[CandidateEntity | CandidateRelation],
        *,
        expected_job_id: str | None = None,
        expected_job_attempt: int | None = None,
        expected_job_owner: str | None = None,
    ) -> None:
        self.candidates.replace_draft_candidates_for_paper(
            ingestion_id,
            paper_id,
            candidates,
            expected_job_id=expected_job_id,
            expected_job_attempt=expected_job_attempt,
            expected_job_owner=expected_job_owner,
        )

    def list_candidates(
        self, ingestion_id: str, status: CandidateStatus | None = None
    ) -> list[dict[str, Any]]:
        return self.candidates.list_candidates(ingestion_id, status)

    def list_candidates_page(
        self,
        ingestion_id: str,
        *,
        status: CandidateStatus | None = None,
        kind: CandidateKind | None = None,
        paper_id: str | None = None,
        min_confidence: float | None = None,
        offset: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        page = self.candidates.list_candidates_page(
            ingestion_id,
            status=status,
            kind=kind,
            paper_id=paper_id,
            min_confidence=min_confidence,
            offset=offset,
            limit=limit,
        )
        drafts = [
            item["candidate"]
            for item in page["items"]
            if item["kind"] == "entity" and item["candidate"]["status"] == "draft"
        ]
        live = self.live_merge_suggestions(drafts)
        for candidate in drafts:
            candidate["merge_suggestions"] = [
                item.model_dump() for item in live.get(candidate["id"], [])
            ]
        return page

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        return self.candidates.get_candidate(candidate_id)

    def get_review_decision(self, candidate_id: str) -> dict[str, Any] | None:
        return self.candidates.get_review_decision(candidate_id)

    def update_candidate(self, candidate_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self.candidates.update_candidate(candidate_id, changes)

    def set_merge_suggestions(
        self, candidate_id: str, suggestions: list[MergeSuggestion]
    ) -> dict[str, Any]:
        return self.candidates.set_merge_suggestions(candidate_id, suggestions)

    def set_prereview(self, candidate_id: str, record: dict[str, Any]) -> None:
        self.candidates.set_prereview(candidate_id, record)

    # Transactional review and outbox (CandidateRepository, ProjectionRepository) --

    def reject_candidate(
        self,
        candidate_id: str,
        review_note: str | None = None,
        *,
        domain_plugin_key: str | None = None,
    ) -> dict[str, Any]:
        return self.candidates.reject_candidate(
            candidate_id, review_note, domain_plugin_key=domain_plugin_key
        )

    def defer_candidate(
        self,
        candidate_id: str,
        review_note: str | None = None,
        *,
        domain_plugin_key: str | None = None,
    ) -> dict[str, Any]:
        return self.candidates.defer_candidate(
            candidate_id, review_note, domain_plugin_key=domain_plugin_key
        )

    def record_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        status: CandidateStatus,
        canonical_id: str | None = None,
    ) -> dict[str, Any]:
        return self.candidates.record_decision(
            candidate_id, decision=decision, status=status, canonical_id=canonical_id
        )

    def publish_entity(
        self,
        candidate_id: str,
        canonical_id: str | None = None,
        *,
        review_note: str | None = None,
        decision_name: str | None = None,
        domain_plugin_key: str | None = None,
        core_domain: str = "",
        core_claim_override: CoreEntityClaimOverride | None = None,
    ) -> PublishedEntity:
        return self.candidates.publish_entity(
            candidate_id,
            canonical_id,
            review_note=review_note,
            decision_name=decision_name,
            domain_plugin_key=domain_plugin_key,
            core_domain=core_domain,
            core_claim_override=core_claim_override,
        )

    def publish_relation(self, candidate_id: str) -> PublishedRelation:
        return self.candidates.publish_relation(candidate_id)

    def projection_status_for_candidate(self, candidate_id: str) -> str:
        return self.projections.projection_status_for_candidate(candidate_id)

    def claim_projection(
        self,
        lease_seconds: int = 120,
        *,
        owner_id: str | None = None,
    ) -> ProjectionEvent | None:
        return self.projections.claim_projection(lease_seconds, owner_id=owner_id)

    def renew_projection_lease(
        self,
        event_id: str,
        *,
        expected_attempt: int,
        expected_owner: str,
        lease_seconds: int,
    ) -> bool:
        return self.projections.renew_projection_lease(
            event_id,
            expected_attempt=expected_attempt,
            expected_owner=expected_owner,
            lease_seconds=lease_seconds,
        )

    def complete_projection(
        self,
        event_id: str,
        *,
        expected_attempt: int | None = None,
        expected_owner: str | None = None,
    ) -> KnowledgeIngestion:
        ingestion_id = self.projections.complete_projection(
            event_id, expected_attempt=expected_attempt, expected_owner=expected_owner
        )
        return self.get_ingestion(ingestion_id)

    def fail_projection(
        self,
        event_id: str,
        error: str,
        *,
        expected_attempt: int | None = None,
        expected_owner: str | None = None,
    ) -> KnowledgeIngestion:
        ingestion_id = self.projections.fail_projection(
            event_id, error, expected_attempt=expected_attempt, expected_owner=expected_owner
        )
        return self.get_ingestion(ingestion_id)

    def retry_projection(self, event_id: str) -> ProjectionEvent:
        return self.projections.retry_projection(event_id)

    # Published knowledge --------------------------------------------------------

    def find_merge_suggestions(
        self, *, name: str, entity_type: str, aliases: list[str]
    ) -> list[MergeSuggestion]:
        types = sorted(merge_compatible_types(entity_type))
        placeholders = ", ".join("?" for _ in types)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM published_entities "
                f"WHERE entity_type IN ({placeholders})",
                types,
            ).fetchall()
        entities = [PublishedEntity.model_validate_json(row["payload_json"]) for row in rows]
        return _merge_suggestions_against(name, aliases, entities, entity_type)

    def live_merge_suggestions(
        self, candidates: list[dict[str, Any]]
    ) -> dict[str, list[MergeSuggestion]]:
        """Recompute suggestions for draft entity candidates against current knowledge.

        Suggestions stored at extraction time go stale as soon as another
        candidate is published, so review screens read them live.  Published
        entities are loaded once per call rather than once per candidate.
        """

        if not candidates:
            return {}
        by_type: dict[str, list[PublishedEntity]] = {}
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM published_entities").fetchall()
        for row in rows:
            entity = PublishedEntity.model_validate_json(row["payload_json"])
            by_type.setdefault(entity.type, []).append(entity)
        return {
            str(item["id"]): _merge_suggestions_against(
                str(item["name"]),
                list(item.get("aliases") or []),
                [
                    entity
                    for entity_type in sorted(merge_compatible_types(str(item["type"])))
                    for entity in by_type.get(entity_type, [])
                ],
                str(item["type"]),
            )
            for item in candidates
        }

    def duplicate_groups(self, ingestion_id: str) -> dict[str, Any]:
        """Group draft entity candidates that likely denote an existing or shared entity.

        A group either points at a published entity (exact or similar name of
        the same type) or collects drafts that share a normalized name/alias
        while nothing is published yet.  This is read-only: every merge still
        goes through an explicit per-candidate review decision.
        """

        drafts = [
            item["candidate"]
            for item in self.list_candidates(ingestion_id, status="draft")
            if item["kind"] == "entity"
        ]
        suggestions = self.live_merge_suggestions(drafts)
        groups: dict[str, dict[str, Any]] = {}
        unmatched: list[dict[str, Any]] = []
        for candidate in drafts:
            matches = suggestions.get(candidate["id"], [])
            candidate["merge_suggestions"] = [item.model_dump() for item in matches]
            if not matches:
                unmatched.append(candidate)
                continue
            target = matches[0]
            group = groups.setdefault(
                f"entity:{target.entity_id}",
                {
                    "key": f"entity:{target.entity_id}",
                    "name": target.name,
                    "type": target.type,
                    "target": target.model_dump(),
                    "candidates": [],
                },
            )
            group["candidates"].append(candidate)

        # Drafts without any published counterpart: union them by any shared
        # normalized name or alias within the same (or an interchangeable) type.
        parent = list(range(len(unmatched)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        seen: dict[tuple[str, str], int] = {}
        for index, candidate in enumerate(unmatched):
            for label in [candidate["name"], *(candidate.get("aliases") or [])]:
                key = (
                    _merge_type_family(str(candidate["type"])), _normalize_merge_name(str(label)),
                )
                if not key[1]:
                    continue
                if key in seen:
                    parent[find(index)] = find(seen[key])
                else:
                    seen[key] = index
        clusters: dict[int, list[dict[str, Any]]] = {}
        for index, candidate in enumerate(unmatched):
            clusters.setdefault(find(index), []).append(candidate)
        for members in clusters.values():
            if len(members) < 2:
                continue
            first = members[0]
            key = f"draft:{first['type']}:{_normalize_merge_name(first['name'])}"
            groups[key] = {
                "key": key,
                "name": first["name"],
                "type": first["type"],
                "target": None,
                "candidates": members,
            }

        ordered = sorted(
            groups.values(),
            key=lambda group: (
                group["target"] is None,
                -(group["target"] or {}).get("similarity", 0.0),
                -len(group["candidates"]),
                str(group["name"]).casefold(),
            ),
        )
        return {
            "groups": ordered,
            "candidate_count": sum(len(group["candidates"]) for group in ordered),
        }

    def get_published_entity(self, entity_id: str) -> PublishedEntity:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM published_entities WHERE id = ?", (entity_id,)
            ).fetchone()
        if row is None:
            raise KeyError(entity_id)
        return PublishedEntity.model_validate_json(row["payload_json"])

    def get_published_relation(self, relation_id: str) -> PublishedRelation:
        """Return the relation with evidence merged from historical duplicates."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_entity_id, target_entity_id, relation_type "
                "FROM published_relations WHERE id = ?", (relation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(relation_id)
            rows = connection.execute(
                """SELECT payload_json FROM published_relations
                   WHERE source_entity_id = ? AND target_entity_id = ? AND relation_type = ?
                   ORDER BY created_at, id""",
                tuple(row),
            ).fetchall()
        relations = [PublishedRelation.model_validate_json(item[0]) for item in rows]
        merged = relations[0]
        for relation in relations[1:]:
            merged = _merge_relations(merged, relation)
        return merged.model_copy(update={"id": relation_id})

    def list_published_entities(self, topic_slug: str | None = None) -> list[PublishedEntity]:
        query = "SELECT payload_json FROM published_entities"
        params: tuple[Any, ...] = ()
        if topic_slug:
            query = """
                SELECT DISTINCT e.payload_json FROM published_entities e
                JOIN collection_memberships m
                  ON m.aggregate_type = 'entity' AND m.aggregate_id = e.id
                WHERE m.collection_slug = ?
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
                SELECT DISTINCT r.payload_json FROM published_relations r
                JOIN collection_memberships m
                  ON m.aggregate_type = 'relation' AND m.aggregate_id = r.id
                WHERE m.collection_slug = ?
            """
            params = (topic_slug,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            allowed = None
            if topic_slug:
                allowed = {str(row[0]) for row in connection.execute(
                    """SELECT DISTINCT d.document_id FROM ingestion_documents d
                       JOIN ingestion_collections ic ON ic.ingestion_id = d.ingestion_id
                       WHERE ic.collection_slug = ?""", (topic_slug,))}
        grouped: dict[tuple[str, str, str], PublishedRelation] = {}
        for row in rows:
            item = PublishedRelation.model_validate_json(row["payload_json"])
            if allowed is not None:
                item = item.model_copy(update={
                    "evidence": [e for e in item.evidence if e.paper_id in allowed]})
            key = (item.source_entity_id, item.target_entity_id, item.type)
            grouped[key] = _merge_relations(grouped[key], item) if key in grouped else item
        return list(grouped.values())

    def list_topics(self) -> list[dict[str, Any]]:
        return [
            {
                "topic_slug": item.slug,
                "topic": item.name,
                "collection_slug": item.slug,
                "collection": item.name,
                "is_system": item.is_system,
                "updated_at": item.updated_at,
                "ingestion_count": item.ingestion_count,
                "vault_path": None,
            }
            for item in self.list_collections()
        ]

    def entity_detail(self, entity_id: str) -> dict[str, Any]:
        entity = self.get_published_entity(entity_id)
        relations = [
            item
            for item in self.list_published_relations()
            if entity_id in {item.source_entity_id, item.target_entity_id}
        ]
        evidence_paper_ids = {item.paper_id for item in entity.evidence}
        with self._connect() as connection:
            documents = (
                connection.execute(
                    "SELECT id, title, ingestion_id, pages FROM documents WHERE id IN ({})".format(
                        ",".join("?" for _ in evidence_paper_ids) or "''"
                    ),
                    tuple(sorted(evidence_paper_ids)),
                ).fetchall()
                if evidence_paper_ids
                else []
            )
            mentions = connection.execute(
                "SELECT payload_json FROM source_mentions WHERE candidate_id IN "
                "(SELECT id FROM candidates WHERE canonical_id = ?)",
                (entity_id,),
            ).fetchall()
        papers = []
        for paper in self.list_published_entities():
            if paper.type == "Paper" and any(
                evidence.paper_id in evidence_paper_ids for evidence in paper.evidence
            ):
                papers.append(paper.model_dump())
        sense_row = None
        with self._connect() as connection:
            sense_row = connection.execute(
                "SELECT payload_json FROM concept_senses WHERE id = ?", (entity_id,)
            ).fetchone()
        return {
            "entity": entity.model_dump(),
            "concept_sense": json.loads(sense_row["payload_json"]) if sense_row else None,
            "relations": [item.model_dump() for item in relations],
            "documents": [dict(item) for item in documents],
            "papers": papers,
            "source_mentions": [json.loads(item["payload_json"]) for item in mentions],
        }

    def list_source_mentions(self, entity_id: str | None = None) -> list[SourceMention]:
        query = "SELECT m.payload_json FROM source_mentions m"
        params: tuple[Any, ...] = ()
        if entity_id:
            query += " JOIN mention_sense_links l ON l.mention_id = m.id WHERE l.sense_id = ?"
            params = (entity_id,)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [SourceMention.model_validate_json(item["payload_json"]) for item in rows]

    def get_concept_sense(self, sense_id: str) -> ConceptSense:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM concept_senses WHERE id = ?", (sense_id,)
            ).fetchone()
        if row is None:
            raise KeyError(sense_id)
        return ConceptSense.model_validate_json(row["payload_json"])

    def _backfill_semantic_records(self) -> None:
        """Create provenance records for pre-semantic-layer publications without altering facts."""
        with self._connect() as connection:
            entities = connection.execute("SELECT * FROM published_entities").fetchall()
            for row in entities:
                entity = PublishedEntity.model_validate_json(row["payload_json"])
                now = _now()
                refresh_aggregate_collections_tx(connection, "entity", entity.id, now)
                sense = ConceptSense(
                    id=entity.id,
                    name=entity.name,
                    type=entity.type,
                    definition=entity.summary,
                    aliases=entity.aliases,
                    status="legacy",
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO concept_senses
                        (id, normalized_name, entity_type, status, payload_json,
                         created_at, updated_at)
                    VALUES (?, ?, ?, 'legacy', ?, ?, ?)
                    """,
                    (
                        entity.id,
                        _normalize(entity.name),
                        entity.type,
                        _dump(sense.model_dump()),
                        now,
                        now,
                    ),
                )
            relation_ids = connection.execute("SELECT id FROM published_relations").fetchall()
            for row in relation_ids:
                refresh_aggregate_collections_tx(
                    connection, "relation", str(row["id"]), _now()
                )
            candidates = connection.execute(
                """
                SELECT * FROM candidates WHERE kind = 'entity'
                  AND status IN ('published', 'merged', 'deferred')
                """
            ).fetchall()
            for row in candidates:
                stored = candidate_from_row(row)
                candidate = CandidateEntity.model_validate(stored["candidate"])
                mention_status = "source_only" if candidate.status == "deferred" else "linked"
                mention_id = upsert_source_mention_tx(connection, candidate, mention_status)
                if candidate.canonical_id:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO mention_sense_links
                            (mention_id, sense_id, link_kind, created_at)
                        VALUES (?, ?, 'legacy', ?)
                        """,
                        (mention_id, candidate.canonical_id, _now()),
                    )

    def published_paper_ids(self, topic_slugs: list[str] | None = None) -> set[str]:
        shadow = self.knowledge_core_shadow_read(topic_slugs)
        if shadow["cutover_ready"]:
            return set(shadow["authorized_core_paper_ids"])
        return set(shadow["legacy_paper_ids"])

    def knowledge_core_shadow_read(
        self, topic_slugs: list[str] | None = None
    ) -> dict[str, Any]:
        """Compare compatibility and formal Core authorization without changing either."""

        entities: list[PublishedEntity] = []
        if topic_slugs:
            for slug in topic_slugs:
                entities.extend(self.list_published_entities(slug))
        else:
            entities = self.list_published_entities()
        legacy_paper_ids = {
            evidence.paper_id
            for entity in entities
            if entity.type == "Paper"
            for evidence in entity.evidence
            if evidence.paper_id
        }
        core_paper_ids = self.core_repository.published_paper_ids()
        authorized_core = (
            core_paper_ids.intersection(legacy_paper_ids) if topic_slugs else core_paper_ids
        )
        compared_core = authorized_core if topic_slugs else core_paper_ids
        legacy_only = legacy_paper_ids - compared_core
        core_only = compared_core - legacy_paper_ids
        backfill_ready = self.core_repository.is_v0009_backfill_ready()
        return {
            "backfill_ready": backfill_ready,
            "cutover_ready": backfill_ready and not legacy_only and not core_only,
            "legacy_paper_ids": sorted(legacy_paper_ids),
            "core_paper_ids": sorted(core_paper_ids),
            "authorized_core_paper_ids": sorted(authorized_core),
            "legacy_only": sorted(legacy_only),
            "core_only": sorted(core_only),
        }

    def list_projection_chunks(
        self, collection_slug: str | None = None
    ) -> list[DocumentChunk]:
        """Read only formal-paper chunks for rebuilding the vector projection."""

        allowed = self.published_paper_ids([collection_slug] if collection_slug else None)
        if not allowed:
            return []
        placeholders = ", ".join("?" for _ in allowed)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunk.*, document.title
                FROM chunks chunk
                JOIN documents document ON document.id = chunk.document_id
                WHERE chunk.document_id IN ({placeholders})
                  AND document.content_status IN ('available', 'qdrant_backfilled')
                ORDER BY chunk.document_id, chunk.chunk_index, chunk.id
                """,
                tuple(sorted(allowed)),
            ).fetchall()
        paper_scopes: dict[str, set[str]] = {}
        for entity in self.list_published_entities():
            if entity.type != "Paper":
                continue
            scopes = set(entity.collection_slugs or entity.topic_slugs)
            for evidence in entity.evidence:
                paper_scopes.setdefault(evidence.paper_id, set()).update(scopes)
        chunks: list[DocumentChunk] = []
        for row in rows:
            content = str(row["content"] or "")
            if (
                not content.strip()
                or sha256(content.encode("utf-8")).hexdigest()
                != str(row["content_sha256"])
            ):
                continue
            metadata = json.loads(row["metadata_json"] or "{}")
            scopes = sorted(paper_scopes.get(str(row["document_id"]), set()))
            chunks.append(
                DocumentChunk(
                    id=str(row["id"]),
                    paper_id=str(row["document_id"]),
                    title=str(row["title"] or "未命名论文").strip() or "未命名论文",
                    text=content,
                    chunk_index=int(row["chunk_index"]),
                    token_count=max(1, len(content.split())),
                    source_tier=metadata.get("source_tier", "primary_fulltext"),
                    metadata={
                        **metadata,
                        "page_start": int(row["page_start"]),
                        "page_end": int(row["page_end"]),
                        "collection_slugs": scopes,
                    },
                )
            )
        return chunks

    def refresh_ingestion_status(self, ingestion_id: str) -> KnowledgeIngestion:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            refresh_ingestion_status_tx(connection, ingestion_id, now=_now())
        return self.get_ingestion(ingestion_id)

    def finalize_ingestion_execution(
        self,
        ingestion_id: str,
        job_id: str,
        *,
        expected_attempt: int,
        status: Literal["needs_review", "failed"],
        error: str | None = None,
        expected_owner: str | None = None,
    ) -> KnowledgeIngestion:
        """Fence and commit an ingestion plus its durable job as one terminal write."""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                """
                SELECT 1 FROM knowledge_jobs
                WHERE id = ? AND kind = 'ingestion' AND resource_id = ?
                  AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (job_id, ingestion_id, expected_attempt, expected_owner, expected_owner),
            ).fetchone()
            if owned is None:
                raise StaleIngestionExecution(
                    f"Ingestion {ingestion_id} is no longer owned by attempt {expected_attempt}."
                )
            connection.execute(
                """
                UPDATE ingestions SET status = ?, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, ingestion_id),
            )
            job_status = "completed" if status == "needs_review" else "failed"
            job_error = (error or "知识入库失败")[:4000] if job_status == "failed" else None
            updated = connection.execute(
                """
                UPDATE knowledge_jobs
                SET status = ?, lease_until = NULL, lease_owner = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND attempts = ?
                  AND (? IS NULL OR lease_owner = ?)
                """,
                (
                    job_status,
                    job_error,
                    now,
                    job_id,
                    expected_attempt,
                    expected_owner,
                    expected_owner,
                ),
            )
            if updated.rowcount != 1:
                raise StaleIngestionExecution(
                    f"Ingestion {ingestion_id} is no longer owned by attempt {expected_attempt}."
                )
        return self.get_ingestion(ingestion_id)

    # Row conversion -------------------------------------------------------------

    def _ingestion_from_row(self, row: sqlite3.Row) -> KnowledgeIngestion:
        with self._connect() as connection:
            collection_row = connection.execute(
                """
                SELECT c.name, c.slug FROM ingestion_collections ic
                JOIN knowledge_collections c ON c.slug = ic.collection_slug
                WHERE ic.ingestion_id = ?
                """,
                (row["id"],),
            ).fetchone()
            document_count = connection.execute(
                "SELECT COUNT(*) FROM ingestion_documents WHERE ingestion_id = ?",
                (row["id"],),
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
            job_row = connection.execute(
                """
                SELECT id, status, attempts, lease_until, created_at
                FROM knowledge_jobs
                WHERE kind = 'ingestion' AND resource_id = ?
                """,
                (row["id"],),
            ).fetchone()
            queue_position = None
            if job_row is not None and job_row["status"] == "queued":
                queue_position = connection.execute(
                    """
                    SELECT COUNT(*) FROM knowledge_jobs
                    WHERE kind = 'ingestion' AND status = 'queued'
                      AND (created_at < ? OR (created_at = ? AND id <= ?))
                    """,
                    (job_row["created_at"], job_row["created_at"], job_row["id"]),
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
            collection=(collection_row["name"] if collection_row else row["topic"]),
            collection_slug=(collection_row["slug"] if collection_row else row["topic_slug"]),
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
            job_status=(str(job_row["status"]) if job_row is not None else None),
            job_attempts=(int(job_row["attempts"]) if job_row is not None else 0),
            queue_position=queue_position,
            pending_projection_count=pending_projection_count,
            failed_projection_count=failed_projection_count,
        )

def slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.strip().lower())
    return normalized.strip("-")[:80] or "untitled-topic"


# Extraction often labels the same idea as a concept in one paper and a method
# or task in another.  These types may be offered as merge targets for each
# other (clearly flagged); Paper/Dataset/Metric and the rest stay strict.
_INTERCHANGEABLE_TYPES = frozenset({"Concept", "Method", "Task"})


def merge_compatible_types(entity_type: str) -> frozenset[str]:
    """Entity types whose published entities may be suggested as merge targets."""

    if entity_type in _INTERCHANGEABLE_TYPES:
        return _INTERCHANGEABLE_TYPES
    return frozenset({entity_type})


def _merge_type_family(entity_type: str) -> str:
    return "Concept|Method|Task" if entity_type in _INTERCHANGEABLE_TYPES else entity_type


def _normalize_merge_name(value: str) -> str:
    return _normalize(unicodedata.normalize("NFKC", value))


def _merge_suggestions_against(
    name: str,
    aliases: list[str],
    entities: list[PublishedEntity],
    candidate_type: str | None = None,
) -> list[MergeSuggestion]:
    """Match one candidate label set against compatible published entities.

    Entities of a different (interchangeable) type are flagged with
    ``type_differs`` and rank after same-type matches of equal similarity.
    """

    normalized = {_normalize_merge_name(name), *(_normalize_merge_name(alias) for alias in aliases)}
    matches: list[MergeSuggestion] = []
    for entity in entities:
        type_differs = candidate_type is not None and entity.type != candidate_type
        known = {
            _normalize_merge_name(entity.name),
            *(_normalize_merge_name(alias) for alias in entity.aliases),
        }
        if normalized.intersection(known):
            matches.append(
                MergeSuggestion(
                    entity_id=entity.id,
                    name=entity.name,
                    type=entity.type,
                    similarity=1.0,
                    match_kind="exact",
                    type_differs=type_differs,
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
                    type_differs=type_differs,
                )
            )
    return sorted(
        matches,
        key=lambda item: (-item.similarity, item.type_differs, item.name.casefold()),
    )


def _normalized_sources(values: list[str]) -> list[str]:
    """Normalize a multi-PDF submission so URL order cannot create a duplicate job."""

    return sorted(_unique(values))


def _merge_relations(first: PublishedRelation, other: PublishedRelation) -> PublishedRelation:
    """Read-side merge of historical duplicates; stored publication records stay unchanged."""
    return first.model_copy(update={
        "evidence": list({(e.chunk_id, e.quote): e for e in first.evidence + other.evidence}
                         .values()),
        "topic_slugs": list(dict.fromkeys(first.topic_slugs + other.topic_slugs)),
        "collection_slugs": list(dict.fromkeys(first.collection_slugs + other.collection_slugs)),
        "confidence": max(first.confidence, other.confidence),
    })


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


_COLLECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_collections (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    is_system INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO knowledge_collections (slug, name, is_system, created_at, updated_at)
VALUES ('inbox', '收件箱', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO knowledge_collections (slug, name, is_system, created_at, updated_at)
SELECT topic_slug, topic, 0, created_at, updated_at FROM ingestions;
CREATE TABLE IF NOT EXISTS ingestion_collections (
    ingestion_id TEXT PRIMARY KEY REFERENCES ingestions(id),
    collection_slug TEXT NOT NULL REFERENCES knowledge_collections(slug)
);
INSERT OR IGNORE INTO ingestion_collections (ingestion_id, collection_slug)
SELECT id, topic_slug FROM ingestions;
CREATE TABLE IF NOT EXISTS collection_memberships (
    aggregate_type TEXT NOT NULL CHECK (aggregate_type IN ('entity', 'relation')),
    aggregate_id TEXT NOT NULL,
    ingestion_id TEXT NOT NULL REFERENCES ingestions(id),
    collection_slug TEXT NOT NULL REFERENCES knowledge_collections(slug),
    created_at TEXT NOT NULL,
    PRIMARY KEY (aggregate_type, aggregate_id, ingestion_id)
);
CREATE INDEX IF NOT EXISTS collection_memberships_scope_idx
    ON collection_memberships(collection_slug, aggregate_type, aggregate_id);
INSERT OR IGNORE INTO collection_memberships
    (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
SELECT 'entity', canonical_id, ingestion_id, json_extract(payload_json, '$.topic_slug'), created_at
FROM candidates
WHERE kind = 'entity' AND canonical_id IS NOT NULL AND status IN ('published', 'merged');
INSERT OR IGNORE INTO collection_memberships
    (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
SELECT aggregate_type, aggregate_id, ingestion_id, topic_slug, created_at
FROM projection_outbox WHERE aggregate_type = 'relation';
"""


_SEMANTIC_LAYER_SCHEMA = """
DROP INDEX IF EXISTS published_entities_name_idx;
CREATE TABLE IF NOT EXISTS source_mentions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(id),
    ingestion_id TEXT NOT NULL REFERENCES ingestions(id),
    paper_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_mentions_paper_idx ON source_mentions(paper_id, status);
CREATE TABLE IF NOT EXISTS concept_senses (
    id TEXT PRIMARY KEY,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS concept_senses_name_idx ON concept_senses(normalized_name, entity_type);
CREATE TABLE IF NOT EXISTS mention_sense_links (
    mention_id TEXT NOT NULL REFERENCES source_mentions(id),
    sense_id TEXT NOT NULL REFERENCES concept_senses(id),
    link_kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (mention_id, sense_id)
);
"""


_COLLECTION_RELATION_BACKFILL_SCHEMA = """
INSERT OR IGNORE INTO collection_memberships
    (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
SELECT DISTINCT
    'relation', r.id, document.ingestion_id, membership.collection_slug, r.created_at
FROM published_relations r
JOIN relation_topics legacy ON legacy.relation_id = r.id
JOIN documents document
  ON document.id = json_extract(r.payload_json, '$.evidence[0].paper_id')
JOIN ingestion_collections membership ON membership.ingestion_id = document.ingestion_id;
"""
