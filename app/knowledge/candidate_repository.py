from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from app.knowledge.core_models import CoreEntityClaimOverride
from app.knowledge.core_repository import ClaimEvidenceValidationError, KnowledgeCoreRepository
from app.knowledge.projection_repository import ProjectionRepository
from app.knowledge.schemas import (
    CandidateEntity,
    CandidateRelation,
    CandidateStatus,
    ConceptSense,
    MergeSuggestion,
    PublishedEntity,
    PublishedRelation,
)
from app.persistence.sqlite import SQLiteDatabase

CandidateKind = Literal["entity", "relation"]


class DecisionAlreadyApplied(ValueError):
    """Raised when a concurrent reviewer already moved a candidate out of draft."""

    def __init__(self, candidate_id: str) -> None:
        super().__init__(f"candidate {candidate_id} already has a terminal decision")
        self.candidate_id = candidate_id


class StaleIngestionExecution(RuntimeError):
    """Raised when an expired ingestion executor tries to write after a newer claim."""


class CandidateRepository:
    """Extraction documents, candidate review and transactional publication.

    Shares the Knowledge SQLite database with ``KnowledgeRepository``.  Each
    review decision writes the candidate, review event, legacy publication,
    Knowledge Core synchronization and projection outbox event in one
    ``BEGIN IMMEDIATE`` transaction.
    """

    def __init__(
        self,
        database: SQLiteDatabase,
        core_repository: KnowledgeCoreRepository,
        projections: ProjectionRepository,
    ) -> None:
        self.database = database
        self.core_repository = core_repository
        self.projections = projections

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

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
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO documents (
                    id, ingestion_id, title, source, source_url, local_path, pages, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    source = excluded.source,
                    source_url = COALESCE(excluded.source_url, documents.source_url),
                    local_path = COALESCE(NULLIF(excluded.local_path, ''), documents.local_path),
                    pages = excluded.pages,
                    metadata_json = excluded.metadata_json
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
            connection.execute(
                """
                INSERT OR IGNORE INTO ingestion_documents (
                    ingestion_id, document_id, created_at
                ) VALUES (?, ?, ?)
                """,
                (ingestion_id, document_id, _now()),
            )

    def add_candidate_entity(self, candidate: CandidateEntity) -> None:
        self._add_candidate("entity", candidate)

    def add_candidate_relation(self, candidate: CandidateRelation) -> None:
        self._add_candidate("relation", candidate)

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
        """Replace one paper's unreviewed extraction as a fenced, atomic batch."""
        if (expected_job_id is None) != (expected_job_attempt is None):
            raise ValueError("A claimed ingestion requires both its job id and attempt.")
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if expected_job_id is not None and expected_job_attempt is not None:
                owned = connection.execute(
                    """
                    SELECT 1 FROM knowledge_jobs
                    WHERE id = ? AND kind = 'ingestion' AND resource_id = ?
                      AND status = 'running' AND attempts = ?
                      AND (? IS NULL OR lease_owner = ?)
                    """,
                    (
                        expected_job_id,
                        ingestion_id,
                        expected_job_attempt,
                        expected_job_owner,
                        expected_job_owner,
                    ),
                ).fetchone()
                if owned is None:
                    raise StaleIngestionExecution(
                        f"Ingestion {ingestion_id} is no longer owned by attempt "
                        f"{expected_job_attempt}."
                    )
            reviewed = connection.execute(
                """
                SELECT COUNT(*) FROM candidates
                WHERE ingestion_id = ?
                  AND json_extract(payload_json, '$.evidence.paper_id') = ?
                  AND status <> 'draft'
                """,
                (ingestion_id, paper_id),
            ).fetchone()[0]
            if reviewed:
                raise ValueError(
                    "cannot replace an ingestion paper whose candidates were already reviewed"
                )
            connection.execute(
                """
                DELETE FROM candidates
                WHERE ingestion_id = ?
                  AND json_extract(payload_json, '$.evidence.paper_id') = ?
                  AND status = 'draft'
                """,
                (ingestion_id, paper_id),
            )
            for candidate in candidates:
                kind: CandidateKind = (
                    "relation" if isinstance(candidate, CandidateRelation) else "entity"
                )
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
        return [candidate_from_row(row) for row in rows]

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
        """Return a bounded review page while keeping the legacy list API intact.

        Review queues can contain hundreds of relations.  The React workspace
        needs a small, stable page rather than materialising every candidate in
        the browser.  Entity names are resolved server-side for relation cards
        so reviewers never have to interpret internal candidate identifiers.
        """

        clauses = ["ingestion_id = ?"]
        params: list[Any] = [ingestion_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if paper_id is not None:
            clauses.append("json_extract(payload_json, '$.evidence.paper_id') = ?")
            params.append(paper_id)
        if min_confidence is not None:
            clauses.append("CAST(json_extract(payload_json, '$.confidence') AS REAL) >= ?")
            params.append(min_confidence)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM candidates WHERE {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                (
                    f"SELECT * FROM candidates WHERE {where} "
                    "ORDER BY kind, created_at LIMIT ? OFFSET ?"
                ),
                [*params, limit, offset],
            ).fetchall()
            entity_rows = connection.execute(
                "SELECT * FROM candidates WHERE ingestion_id = ? AND kind = 'entity'",
                (ingestion_id,),
            ).fetchall()
            canonical_ids = sorted(
                {str(row["canonical_id"]) for row in entity_rows if row["canonical_id"]}
            )
            canonical_names = {
                str(row["id"]): str(json.loads(row["payload_json"]).get("name") or "")
                for row in (
                    connection.execute(
                        "SELECT id, payload_json FROM published_entities WHERE id IN ({})".format(
                            ",".join("?" for _ in canonical_ids)
                        ),
                        canonical_ids,
                    ).fetchall()
                    if canonical_ids
                    else []
                )
            }

        entity_candidates = [candidate_from_row(row)["candidate"] for row in entity_rows]
        names = {item["id"]: item.get("name", "未命名实体") for item in entity_candidates}
        # A merged endpoint attaches the relation to its canonical entity; show
        # that name so the reviewer sees where the edge will actually land.
        endpoint_canonical = {
            item["id"]: canonical_names.get(item.get("canonical_id") or "")
            for item in entity_candidates
        }
        items = [candidate_from_row(row) for row in rows]
        for item in items:
            if item["kind"] != "relation":
                continue
            candidate = item["candidate"]
            for side in ("source", "target"):
                endpoint_id = candidate[f"{side}_candidate_id"]
                candidate[f"{side}_name"] = names.get(endpoint_id, "未解析实体")
                canonical_name = endpoint_canonical.get(endpoint_id)
                if canonical_name and canonical_name != candidate[f"{side}_name"]:
                    candidate[f"{side}_canonical_name"] = canonical_name
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return candidate_from_row(row)

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
            {"name", "summary", "aliases", "sense_qualifier", "confidence"}
            if stored["kind"] == "entity"
            else {"summary", "confidence"}
        )
        payload.update({key: value for key, value in changes.items() if key in allowed})
        candidate = model_for_kind(stored["kind"], payload)
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

    def reject_candidate(
        self,
        candidate_id: str,
        review_note: str | None = None,
        *,
        domain_plugin_key: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = candidate_from_row(row)
            self._require_domain_candidate_owner(stored, domain_plugin_key)
            payload = dict(stored["candidate"])
            payload["status"] = "rejected"
            self._record_decision_tx(
                connection,
                row=row,
                payload=payload,
                decision="reject",
                status="rejected",
                canonical_id=None,
                review_note=review_note,
            )
        return self.get_candidate(candidate_id)

    def defer_candidate(
        self,
        candidate_id: str,
        review_note: str | None = None,
        *,
        domain_plugin_key: str | None = None,
    ) -> dict[str, Any]:
        """Keep a source-scoped assertion without publishing a graph fact."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = candidate_from_row(row)
            self._require_domain_candidate_owner(stored, domain_plugin_key)
            payload = dict(stored["candidate"])
            payload["status"] = "deferred"
            self._record_decision_tx(
                connection,
                row=row,
                payload=payload,
                decision="defer",
                status="deferred",
                canonical_id=None,
                review_note=review_note,
            )
            if stored["kind"] == "entity":
                upsert_source_mention_tx(
                    connection, CandidateEntity.model_validate(stored["candidate"]), "source_only"
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
        try:
            return self._publish_entity(
                candidate_id,
                canonical_id,
                review_note=review_note,
                decision_name=decision_name,
                domain_plugin_key=domain_plugin_key,
                core_domain=core_domain,
                core_claim_override=core_claim_override,
            )
        except ClaimEvidenceValidationError as exc:
            self.core_repository.record_attention(
                item_type="candidate_entity",
                item_id=candidate_id,
                reason=str(exc),
            )
            raise

    def _publish_entity(
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = candidate_from_row(row)
            if stored["kind"] != "entity":
                raise ValueError("only entity candidates can be published as entities")
            candidate = CandidateEntity.model_validate(stored["candidate"])
            self._require_domain_candidate_owner(stored, domain_plugin_key)
            if domain_plugin_key and (not core_domain or core_claim_override is None):
                raise ValueError(
                    "Domain Knowledge publication requires Core domain and Claim data."
                )
            target_id = canonical_id or candidate.canonical_id
            now = _now()
            if target_id:
                existing_row = connection.execute(
                    "SELECT payload_json FROM published_entities WHERE id = ?", (target_id,)
                ).fetchone()
                if existing_row is None:
                    raise ValueError("合并目标实体不存在，请刷新后重新选择。")
                existing = PublishedEntity.model_validate_json(existing_row["payload_json"])
                entity = existing.model_copy(
                    update={
                        "aliases": _unique(existing.aliases + [candidate.name] + candidate.aliases),
                        "evidence": _unique_models(existing.evidence + [candidate.evidence]),
                        "topic_slugs": _unique(existing.topic_slugs + [candidate.topic_slug]),
                        "collection_slugs": _unique(
                            existing.collection_slugs + [candidate.topic_slug]
                        ),
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
                    collection_slugs=[candidate.topic_slug],
                    metadata={
                        **candidate.metadata,
                        "sense_qualifier": candidate.sense_qualifier,
                    },
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
                    raise ValueError("无法发布实体，请检查规范名称与数据完整性。") from exc
                status = "published"
                decision = "approve"
            connection.execute(
                "INSERT OR IGNORE INTO entity_topics (entity_id, topic_slug) VALUES (?, ?)",
                (entity.id, candidate.topic_slug),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO collection_memberships
                    (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
                VALUES ('entity', ?, ?, ?, ?)
                """,
                (entity.id, candidate.ingestion_id, candidate.topic_slug, now),
            )
            refresh_aggregate_collections_tx(connection, "entity", entity.id, now)
            upsert_source_mention_tx(connection, candidate, "linked")
            self._upsert_concept_sense_tx(connection, entity, candidate)
            if core_claim_override is not None:
                source_version = str(core_claim_override.properties.get("source_version") or "")
                if source_version:
                    self.core_repository.require_source_version_for_evidence_tx(
                        connection, candidate.evidence, version=source_version
                    )
            self.core_repository.synchronize_published_entity_tx(
                connection,
                entity,
                domain=core_domain,
                claim_override=core_claim_override,
            )
            self.core_repository.resolve_attention_tx(
                connection, item_type="candidate_entity", item_id=candidate.id
            )
            payload = candidate.model_dump()
            payload.update({"status": status, "canonical_id": entity.id})
            self._record_decision_tx(
                connection,
                row=row,
                payload=payload,
                decision=decision_name or decision,
                status=status,
                canonical_id=entity.id,
                review_note=review_note,
            )
            self.projections.enqueue_projection_tx(
                connection,
                ingestion_id=candidate.ingestion_id,
                candidate_id=candidate.id,
                aggregate_type="entity",
                aggregate_id=entity.id,
                topic_slug=candidate.topic_slug,
            )
        return entity

    def publish_relation(self, candidate_id: str) -> PublishedRelation:
        try:
            return self._publish_relation(candidate_id)
        except ClaimEvidenceValidationError as exc:
            self.core_repository.record_attention(
                item_type="candidate_relation",
                item_id=candidate_id,
                reason=str(exc),
            )
            raise

    def _publish_relation(self, candidate_id: str) -> PublishedRelation:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._candidate_row_tx(connection, candidate_id)
            if row["status"] != "draft":
                raise DecisionAlreadyApplied(candidate_id)
            stored = candidate_from_row(row)
            if stored["kind"] != "relation":
                raise ValueError("only relation candidates can be published as relations")
            candidate = CandidateRelation.model_validate(stored["candidate"])
            source_row = self._candidate_row_tx(connection, candidate.source_candidate_id)
            target_row = self._candidate_row_tx(connection, candidate.target_candidate_id)
            source = CandidateEntity.model_validate(
                candidate_from_row(source_row)["candidate"]
            )
            target = CandidateEntity.model_validate(
                candidate_from_row(target_row)["candidate"]
            )
            if not source.canonical_id or not target.canonical_id:
                raise ValueError("请先批准或合并关系两端的实体，再发布这条关系。")
            if source.canonical_id == target.canonical_id:
                raise ValueError("关系两端已合并为同一个实体，无法发布为关系，请驳回或搁置。")
            relation = PublishedRelation(
                id=f"relation-{uuid4().hex}",
                source_entity_id=source.canonical_id,
                target_entity_id=target.canonical_id,
                type=candidate.type,
                summary=candidate.summary,
                confidence=candidate.confidence,
                evidence=[candidate.evidence],
                topic_slugs=[candidate.topic_slug],
                collection_slugs=[candidate.topic_slug],
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
            connection.execute(
                """
                INSERT OR IGNORE INTO collection_memberships
                    (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
                VALUES ('relation', ?, ?, ?, ?)
                """,
                (relation.id, candidate.ingestion_id, candidate.topic_slug, now),
            )
            refresh_aggregate_collections_tx(connection, "relation", relation.id, now)
            self.core_repository.synchronize_published_relation_tx(connection, relation)
            self.core_repository.resolve_attention_tx(
                connection, item_type="candidate_relation", item_id=candidate.id
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
                review_note=None,
            )
            self.projections.enqueue_projection_tx(
                connection,
                ingestion_id=candidate.ingestion_id,
                candidate_id=candidate.id,
                aggregate_type="relation",
                aggregate_id=relation.id,
                topic_slug=candidate.topic_slug,
            )
        return relation

    @staticmethod
    def _require_domain_candidate_owner(
        stored: dict[str, Any], domain_plugin_key: str | None
    ) -> None:
        """Keep plugin-owned candidates on their explicit review route."""

        candidate = stored["candidate"]
        metadata = candidate.get("metadata") if isinstance(candidate, dict) else None
        requested_plugin = str(
            (metadata.get("domain_review_route") if isinstance(metadata, dict) else "") or ""
        ).strip()
        if requested_plugin and requested_plugin != domain_plugin_key:
            raise ValueError(
                "Domain Knowledge candidates must be reviewed through their Domain Plugin."
            )
        if domain_plugin_key and requested_plugin != domain_plugin_key:
            raise ValueError("Candidate is not owned by the requested Domain Plugin.")

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
        review_note: str | None = None,
    ) -> None:
        candidate = model_for_kind(row["kind"], payload)
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
        event_payload = candidate.model_dump()
        if review_note:
            event_payload["review_note"] = review_note
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
                _dump(event_payload),
                _now(),
            ),
        )

    def _upsert_concept_sense_tx(
        self, connection: sqlite3.Connection, entity: PublishedEntity, candidate: CandidateEntity
    ) -> None:
        mention_id = f"mention-{candidate.id}"
        existing = connection.execute(
            "SELECT payload_json FROM concept_senses WHERE id = ?", (entity.id,)
        ).fetchone()
        if existing:
            payload = json.loads(existing["payload_json"])
            payload["aliases"] = _unique(payload.get("aliases", []) + entity.aliases)
            payload["source_mention_ids"] = _unique(
                payload.get("source_mention_ids", []) + [mention_id]
            )
        else:
            payload = ConceptSense(
                id=entity.id,
                name=entity.name,
                type=entity.type,
                qualifier=candidate.sense_qualifier,
                definition=entity.summary,
                scope=candidate.paper_context or candidate.role,
                aliases=entity.aliases,
                status="published",
                source_mention_ids=[mention_id],
            ).model_dump()
        now = _now()
        connection.execute(
            """
            INSERT INTO concept_senses
                (id, normalized_name, entity_type, status, payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                entity.id,
                _normalize(entity.name),
                entity.type,
                "published",
                _dump(payload),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO mention_sense_links (mention_id, sense_id, link_kind, created_at)
            VALUES (?, ?, 'linked', ?)
            """,
            (mention_id, entity.id, now),
        )


def candidate_from_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    payload["status"] = row["status"]
    payload["canonical_id"] = row["canonical_id"]
    model = model_for_kind(row["kind"], payload)
    return {"kind": row["kind"], "candidate": model.model_dump()}


def model_for_kind(
    kind: str, payload: dict[str, Any]
) -> CandidateEntity | CandidateRelation:
    if kind == "entity":
        return CandidateEntity.model_validate(payload)
    payload.pop("canonical_id", None)
    return CandidateRelation.model_validate(payload)


def upsert_source_mention_tx(
    connection: sqlite3.Connection, candidate: CandidateEntity, status: str
) -> str:
    mention_id = f"mention-{candidate.id}"
    now = _now()
    payload = {
        "id": mention_id,
        "candidate_id": candidate.id,
        "ingestion_id": candidate.ingestion_id,
        "paper_id": candidate.evidence.paper_id,
        "name": candidate.name,
        "type": candidate.type,
        "summary": candidate.summary,
        "sense_qualifier": candidate.sense_qualifier,
        "paper_context": candidate.paper_context,
        "role": candidate.role,
        "conditions": candidate.conditions,
        "aliases": candidate.aliases,
        "evidence": candidate.evidence.model_dump(),
        "status": status,
        "created_at": now,
        "updated_at": now,
    }
    connection.execute(
        """
        INSERT INTO source_mentions
            (id, candidate_id, ingestion_id, paper_id, status, payload_json,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(candidate_id) DO UPDATE SET status = excluded.status,
            payload_json = excluded.payload_json, updated_at = excluded.updated_at
        """,
        (
            mention_id,
            candidate.id,
            candidate.ingestion_id,
            candidate.evidence.paper_id,
            status,
            _dump(payload),
            now,
            now,
        ),
    )
    return mention_id


def refresh_aggregate_collections_tx(
    connection: sqlite3.Connection, aggregate_type: str, aggregate_id: str, now: str
) -> None:
    rows = connection.execute(
        """
        SELECT DISTINCT collection_slug FROM collection_memberships
        WHERE aggregate_type = ? AND aggregate_id = ? ORDER BY collection_slug
        """,
        (aggregate_type, aggregate_id),
    ).fetchall()
    slugs = [str(row["collection_slug"]) for row in rows]
    table = "published_entities" if aggregate_type == "entity" else "published_relations"
    row = connection.execute(
        f"SELECT payload_json FROM {table} WHERE id = ?", (aggregate_id,)
    ).fetchone()
    if row is None:
        return
    payload = json.loads(row["payload_json"])
    payload["topic_slugs"] = slugs
    payload["collection_slugs"] = slugs
    connection.execute(
        f"UPDATE {table} SET payload_json = ?, updated_at = ? WHERE id = ?",
        (_dump(payload), now, aggregate_id),
    )


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
