from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.knowledge.core_models import (
    BackfillSummary,
    CoreClaim,
    CoreEntity,
    CoreEntityClaimOverride,
)
from app.knowledge.schemas import (
    ChunkSearchHit,
    EvidenceSpan,
    PublishedEntity,
    PublishedRelation,
    ReportEvidence,
)
from app.knowledge.source_identity import derive_source_identity
from app.persistence.sqlite import SQLiteDatabase
from app.schemas.documents import DocumentChunk


class ClaimEvidenceValidationError(ValueError):
    """Raised when a formal Core claim cannot be grounded in a valid chunk."""


class KnowledgeCoreRepository:
    """Persistence boundary for the v8 Knowledge Core tables.

    The current Research Knowledge Core keeps its legacy tables and API DTOs.
    This repository deliberately uses new Core IDs and records every historical
    identity in ``legacy_record_map`` so no caller has to reuse a legacy ID.
    Legacy published rows remain the compatibility runtime source during the
    Phase 1A transition; Core is the synchronized migration target and foundation
    for subsequent writes, never a third independent source of truth.
    """

    def __init__(self, path: str, database: SQLiteDatabase | None = None) -> None:
        self.path = path
        self.database = database or SQLiteDatabase(path)

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def record_source_document(
        self,
        *,
        document_id: str,
        title: str,
        uri: str,
        content: str,
        parser_version: str,
        metadata: dict[str, Any],
    ) -> str:
        """Persist a parsed document before it is sent to a projection."""

        now = _now()
        checksum = _sha256(content)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_id = self._ensure_source_tx(
                connection,
                title=title,
                uri=uri,
                source_type="pdf",
                metadata=metadata,
                content_sha256=checksum,
                now=now,
            )
            connection.execute(
                """
                UPDATE documents
                SET source_id = ?, content = ?, content_sha256 = ?, content_status = 'available',
                    parser_version = ?, parsed_at = ?
                WHERE id = ?
                """,
                (
                    source_id,
                    content,
                    checksum,
                    parser_version,
                    now,
                    document_id,
                ),
            )
        return source_id

    def record_attention(
        self,
        *,
        item_type: str,
        item_id: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Durably record a blocked formalization without creating a Core fact."""

        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO knowledge_core_attention_items (
                    id, item_type, item_id, status, reason, details_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'needs_attention', ?, ?, ?, ?)
                ON CONFLICT(item_type, item_id, reason) DO UPDATE SET
                    status = 'needs_attention', details_json = excluded.details_json,
                    updated_at = excluded.updated_at
                """,
                (
                    f"core-attention-{uuid4().hex}",
                    item_type,
                    item_id,
                    reason,
                    _dump(details or {}),
                    now,
                    now,
                ),
            )

    def resolve_attention_tx(
        self, connection: sqlite3.Connection, *, item_type: str, item_id: str
    ) -> None:
        """Resolve prior validation attention after the same item publishes safely."""

        connection.execute(
            """
            UPDATE knowledge_core_attention_items
            SET status = 'resolved', updated_at = ?
            WHERE item_type = ? AND item_id = ? AND status = 'needs_attention'
            """,
            (_now(), item_type, item_id),
        )

    def upsert_chunks(self, chunks: Iterable[DocumentChunk]) -> int:
        count = 0
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for chunk in chunks:
                metadata = dict(chunk.metadata)
                page_start = int(metadata.get("page_start", 1) or 1)
                page_end = int(metadata.get("page_end", page_start) or page_start)
                connection.execute(
                    """
                    INSERT INTO chunks (
                        id, document_id, legacy_chunk_id, content, content_sha256,
                        chunk_index, page_start, page_end, location_json, embedding_ref,
                        metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        document_id = excluded.document_id,
                        legacy_chunk_id = excluded.legacy_chunk_id,
                        content = excluded.content,
                        content_sha256 = excluded.content_sha256,
                        chunk_index = excluded.chunk_index,
                        page_start = excluded.page_start,
                        page_end = excluded.page_end,
                        location_json = excluded.location_json,
                        embedding_ref = excluded.embedding_ref,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chunk.id,
                        chunk.paper_id,
                        chunk.id,
                        chunk.text,
                        _sha256(chunk.text),
                        chunk.chunk_index,
                        page_start,
                        page_end,
                        _dump({"page_start": page_start, "page_end": page_end}),
                        None,
                        _dump(metadata),
                        now,
                        now,
                    ),
                )
                count += 1
        return count

    def synchronize_published_entity(
        self,
        entity: PublishedEntity,
        *,
        domain: str = "",
        claim_override: CoreEntityClaimOverride | None = None,
    ) -> str:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.synchronize_published_entity_tx(
                    connection,
                    entity,
                    domain=domain,
                    claim_override=claim_override,
                )
        except ClaimEvidenceValidationError as exc:
            self.record_attention(
                item_type="published_entity",
                item_id=entity.id,
                reason=str(exc),
            )
            raise

    def synchronize_published_entity_tx(
        self,
        connection: sqlite3.Connection,
        entity: PublishedEntity,
        *,
        domain: str = "",
        claim_override: CoreEntityClaimOverride | None = None,
    ) -> str:
        now = _now()
        self._validate_evidence_collection_tx(connection, entity.evidence)
        entity_id = self._mapped_id_tx(connection, "published_entities", entity.id)
        if entity_id is None:
            entity_id = f"core-entity-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO entities (
                    id, legacy_id, name, normalized_name, entity_type, domain,
                    properties_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'published', ?, ?)
                """,
                (
                    entity_id,
                    entity.id,
                    entity.name,
                    _normalize(entity.name),
                    entity.type,
                    domain,
                    _dump(
                        {
                            "aliases": entity.aliases,
                            "metadata": entity.metadata,
                            "topic_slugs": entity.topic_slugs,
                            "collection_slugs": entity.collection_slugs,
                        }
                    ),
                    now,
                    now,
                ),
            )
            self._insert_mapping_tx(
                connection, "published_entities", entity.id, "entities", entity_id
            )
        else:
            connection.execute(
                """
                UPDATE entities
                SET name = ?, normalized_name = ?, entity_type = ?, domain = ?, properties_json = ?,
                    status = 'published', updated_at = ?
                WHERE id = ?
                """,
                (
                    entity.name,
                    _normalize(entity.name),
                    entity.type,
                    domain,
                    _dump(
                        {
                            "aliases": entity.aliases,
                            "metadata": entity.metadata,
                            "topic_slugs": entity.topic_slugs,
                            "collection_slugs": entity.collection_slugs,
                        }
                    ),
                    now,
                    entity_id,
                ),
            )

        representation = claim_override or CoreEntityClaimOverride(
            subject=entity.name,
            predicate="defines",
            object_value=entity.summary,
            claim_type="entity_definition",
            statement=entity.summary,
            properties={"legacy_entity_id": entity.id},
        )
        claim_id = self._ensure_claim_tx(
            connection,
            legacy_id=f"published_entity_definition:{entity.id}",
            entity_id=entity_id,
            relation_id=None,
            subject=representation.subject,
            predicate=representation.predicate,
            object_value=representation.object_value,
            claim_type=representation.claim_type,
            statement=representation.statement,
            confidence=representation.confidence,
            properties=representation.properties,
            now=now,
        )
        for evidence in entity.evidence:
            self._link_evidence_tx(connection, claim_id, evidence, now)
        return entity_id

    def get_entity(self, entity_id: str) -> CoreEntity:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            raise KeyError(f"Core Entity {entity_id} not found")
        return CoreEntity(
            id=str(row["id"]),
            legacy_id=row["legacy_id"],
            name=str(row["name"]),
            normalized_name=str(row["normalized_name"]),
            entity_type=str(row["entity_type"]),
            domain=str(row["domain"]),
            properties=_load_json(row["properties_json"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_claim(self, claim_id: str) -> CoreClaim:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None:
            raise KeyError(f"Core Claim {claim_id} not found")
        return _core_claim_from_row(row)

    def get_claim_by_legacy_id(self, legacy_id: str) -> CoreClaim:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM claims WHERE legacy_id = ?", (legacy_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Core Claim {legacy_id} not found")
        return _core_claim_from_row(row)

    def source_version_for_evidence(self, evidence: EvidenceSpan) -> str:
        """Read the version of the exact Source supporting an EvidenceSpan."""

        with self._connect() as connection:
            _, source_id, _ = self._validate_evidence_tx(connection, evidence)
            row = connection.execute(
                "SELECT version FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            raise ClaimEvidenceValidationError("Evidence Source is missing")
        return str(row["version"])

    def require_source_version_for_evidence_tx(
        self, connection: sqlite3.Connection, evidence: EvidenceSpan, *, version: str
    ) -> None:
        """Require an Evidence Source to already carry one exact version.

        Source versions are established when a document enters Knowledge Core.
        A downstream domain review must never relabel an existing Source: that
        would mutate provenance and could invalidate previously published
        Claims.  The check remains transaction-local with Claim publication so
        an immutable version cannot be observed then changed before commit.
        """

        if not version.strip():
            raise ValueError("Source version is required for versioned domain evidence")
        _, source_id, _ = self._validate_evidence_tx(connection, evidence)
        row = connection.execute(
            "SELECT version FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if row is None:
            raise ClaimEvidenceValidationError("Evidence Source is missing")
        current = str(row["version"] or "")
        if current != version:
            raise ValueError(
                "Evidence Source version must exactly match the reviewed "
                f"patch version (found {current or 'none'})."
            )

    def synchronize_published_relation(self, relation: PublishedRelation) -> str:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self.synchronize_published_relation_tx(connection, relation)
        except ClaimEvidenceValidationError as exc:
            self.record_attention(
                item_type="published_relation",
                item_id=relation.id,
                reason=str(exc),
            )
            raise

    def is_v0009_backfill_ready(self) -> bool:
        with self._connect() as connection:
            backfill = connection.execute(
                "SELECT status FROM knowledge_core_backfills WHERE version = 9"
            ).fetchone()
            if backfill is None or backfill["status"] != "completed":
                return False
            unresolved = connection.execute(
                """
                SELECT COUNT(*) FROM knowledge_core_backfill_items
                WHERE backfill_version = 9 AND status = 'needs_attention'
                """
            ).fetchone()[0]
        return int(unresolved) == 0

    def published_paper_ids(self, collection_slugs: list[str] | None = None) -> set[str]:
        """Return Core-authorized paper IDs after the explicit backfill is complete."""

        selected = set(collection_slugs or [])
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT entity.properties_json, evidence.location_json
                FROM entities entity
                JOIN claims claim ON claim.entity_id = entity.id
                JOIN claim_evidence_links link ON link.claim_id = claim.id
                JOIN evidences evidence ON evidence.id = link.evidence_id
                WHERE entity.entity_type = 'Paper'
                  AND entity.status = 'published'
                  AND claim.status = 'published'
                """
            ).fetchall()
        paper_ids: set[str] = set()
        for row in rows:
            properties = json.loads(row["properties_json"] or "{}")
            scopes = set(properties.get("collection_slugs", []))
            if selected and not selected.intersection(scopes):
                continue
            location = json.loads(row["location_json"] or "{}")
            paper_id = str(location.get("paper_id") or "")
            if paper_id:
                paper_ids.add(paper_id)
        return paper_ids

    def rehydrate_report_evidence(
        self,
        candidates: list[ChunkSearchHit],
        *,
        allowed_paper_ids: set[str],
    ) -> list[ReportEvidence]:
        """Turn untrusted vector IDs into validated SQLite evidence rows."""

        unique_ids = list(dict.fromkeys(item.chunk_id for item in candidates if item.chunk_id))
        if not unique_ids or not allowed_paper_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunk.id AS chunk_id, chunk.document_id, chunk.content,
                       chunk.content_sha256 AS chunk_sha256,
                       chunk.page_start, chunk.page_end,
                       document.title, document.pages, document.content AS document_content,
                       document.content_sha256 AS document_sha256,
                       document.content_status, document.source_id,
                       source.canonical_uri, source.version,
                       source.content_sha256 AS source_sha256
                FROM chunks chunk
                JOIN documents document ON document.id = chunk.document_id
                JOIN sources source ON source.id = document.source_id
                WHERE chunk.id IN ({placeholders})
                """,
                tuple(unique_ids),
            ).fetchall()
        by_id = {str(row["chunk_id"]): row for row in rows}
        evidence: list[ReportEvidence] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate.chunk_id in seen:
                continue
            row = by_id.get(candidate.chunk_id)
            if row is None or not self._valid_report_chunk(row, allowed_paper_ids):
                continue
            seen.add(candidate.chunk_id)
            evidence.append(
                ReportEvidence(
                    id=f"E{len(evidence) + 1}",
                    paper_id=str(row["document_id"]),
                    chunk_id=candidate.chunk_id,
                    title=str(row["title"] or "未命名论文").strip() or "未命名论文",
                    text=str(row["content"]),
                    page_start=int(row["page_start"]),
                    page_end=int(row["page_end"]),
                    score=float(candidate.score),
                )
            )
        return evidence

    @staticmethod
    def _valid_report_chunk(row: sqlite3.Row, allowed_paper_ids: set[str]) -> bool:
        content = str(row["content"] or "")
        document_content = str(row["document_content"] or "")
        try:
            page_start = int(row["page_start"])
            page_end = int(row["page_end"])
            pages = int(row["pages"])
        except (TypeError, ValueError):
            return False
        return bool(
            str(row["document_id"]) in allowed_paper_ids
            and str(row["content_status"]) in {"available", "qdrant_backfilled"}
            and content.strip()
            and document_content.strip()
            and _sha256(content) == str(row["chunk_sha256"])
            and _sha256(document_content) == str(row["document_sha256"])
            and str(row["document_sha256"]) == str(row["source_sha256"])
            and str(row["source_id"] or "")
            and str(row["canonical_uri"] or "")
            and str(row["version"] or "")
            and 1 <= page_start <= page_end <= pages
        )

    def synchronize_published_relation_tx(
        self, connection: sqlite3.Connection, relation: PublishedRelation
    ) -> str:
        now = _now()
        self._validate_evidence_collection_tx(connection, relation.evidence)
        source_id = self._mapped_id_tx(connection, "published_entities", relation.source_entity_id)
        target_id = self._mapped_id_tx(connection, "published_entities", relation.target_entity_id)
        if source_id is None or target_id is None:
            raise ValueError("Core relation endpoints must be synchronized before the relation.")
        relation_id = self._mapped_id_tx(connection, "published_relations", relation.id)
        if relation_id is None:
            relation_id = f"core-relation-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO relations (
                    id, legacy_id, source_entity_id, target_entity_id, relation_type,
                    domain, properties_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, '', ?, 'published', ?, ?)
                """,
                (
                    relation_id,
                    relation.id,
                    source_id,
                    target_id,
                    relation.type,
                    _dump(
                        {
                            "metadata": relation.metadata,
                            "topic_slugs": relation.topic_slugs,
                            "collection_slugs": relation.collection_slugs,
                        }
                    ),
                    now,
                    now,
                ),
            )
            self._insert_mapping_tx(
                connection, "published_relations", relation.id, "relations", relation_id
            )
        else:
            connection.execute(
                """
                UPDATE relations
                SET source_entity_id = ?, target_entity_id = ?, relation_type = ?,
                    properties_json = ?, status = 'published', updated_at = ?
                WHERE id = ?
                """,
                (
                    source_id,
                    target_id,
                    relation.type,
                    _dump(
                        {
                            "metadata": relation.metadata,
                            "topic_slugs": relation.topic_slugs,
                            "collection_slugs": relation.collection_slugs,
                        }
                    ),
                    now,
                    relation_id,
                ),
            )

        claim_id = self._ensure_claim_tx(
            connection,
            legacy_id=f"published_relation_assertion:{relation.id}",
            entity_id=None,
            relation_id=relation_id,
            subject=source_id,
            predicate=relation.type,
            object_value=target_id,
            claim_type="relation_assertion",
            statement=relation.summary,
            confidence=relation.confidence,
            properties={"legacy_relation_id": relation.id},
            now=now,
        )
        for evidence in relation.evidence:
            self._link_evidence_tx(connection, claim_id, evidence, now)
        return relation_id

    def backfill_legacy_documents(
        self, chunk_payloads: Iterable[dict[str, Any]]
    ) -> BackfillSummary:
        """Run the explicit v0009 data backfill after reading Qdrant payloads.

        The caller supplies Qdrant payloads so this data operation has no hidden
        network side effect.  PDF reparsing is intentionally outside this backfill
        and can later validate stored checksums without changing formal facts.
        """

        payloads = list(chunk_payloads)
        self._mark_backfill_running()
        with self._connect() as connection:
            documents = connection.execute("SELECT * FROM documents ORDER BY id").fetchall()
            entity_rows = connection.execute(
                "SELECT id, payload_json FROM published_entities ORDER BY id"
            ).fetchall()
            relation_rows = connection.execute(
                "SELECT id, payload_json FROM published_relations ORDER BY id"
            ).fetchall()
        known_document_ids = {str(row["id"]) for row in documents}

        for payload in payloads:
            item_id = str(payload.get("id") or payload.get("paper_id") or "unknown")

            def process_chunk(
                connection: sqlite3.Connection, payload: dict[str, Any] = payload
            ) -> tuple[str, str | None]:
                paper_id = str(payload.get("paper_id") or "")
                if paper_id not in known_document_ids:
                    return (
                        "needs_attention",
                        "Qdrant payload does not reference a known SQLite document.",
                    )
                self._upsert_chunk_tx(connection, DocumentChunk.model_validate(payload), _now())
                return "completed", None

            self._run_backfill_checkpoint("chunk", item_id, process_chunk)

        for document in documents:
            document_id = str(document["id"])

            def process_document(
                connection: sqlite3.Connection, document_id: str = document_id
            ) -> tuple[str, str | None]:
                row = connection.execute(
                    "SELECT * FROM documents WHERE id = ?", (document_id,)
                ).fetchone()
                if row is None:
                    return "failed", "Legacy document disappeared during the backfill."
                document_chunks = connection.execute(
                    "SELECT content FROM chunks WHERE document_id = ? ORDER BY chunk_index",
                    (document_id,),
                ).fetchall()
                if not document_chunks:
                    return "needs_attention", "No Qdrant chunks were available for this document."
                content = "\n\n".join(str(chunk["content"]) for chunk in document_chunks)
                checksum = _sha256(content)
                metadata = json.loads(row["metadata_json"] or "{}")
                uri = (
                    row["source_url"]
                    or row["local_path"]
                    or str(metadata.get("original_source") or f"legacy://document/{document_id}")
                )
                source_id = self._ensure_source_tx(
                    connection,
                    title=str(row["title"]),
                    uri=str(uri),
                    source_type=str(row["source"] or "pdf"),
                    metadata=metadata,
                    content_sha256=checksum,
                    now=_now(),
                )
                connection.execute(
                    """
                    UPDATE documents
                    SET source_id = ?, content = ?, content_sha256 = ?,
                        content_status = 'qdrant_backfilled', parser_version = 'legacy-qdrant-v1',
                        parsed_at = ?
                    WHERE id = ?
                    """,
                    (source_id, content, checksum, _now(), document_id),
                )
                return "completed", None

            self._run_backfill_checkpoint("document", document_id, process_document)

        for row in entity_rows:
            entity_id = str(row["id"])

            def process_entity(
                connection: sqlite3.Connection, payload: str = str(row["payload_json"])
            ) -> tuple[str, str | None]:
                self.synchronize_published_entity_tx(
                    connection, PublishedEntity.model_validate_json(payload)
                )
                return "completed", None

            self._run_backfill_checkpoint("entity", entity_id, process_entity)

        for row in relation_rows:
            relation_id = str(row["id"])

            def process_relation(
                connection: sqlite3.Connection, payload: str = str(row["payload_json"])
            ) -> tuple[str, str | None]:
                self.synchronize_published_relation_tx(
                    connection, PublishedRelation.model_validate_json(payload)
                )
                return "completed", None

            self._run_backfill_checkpoint("relation", relation_id, process_relation)

        return self._complete_backfill()

    def _mark_backfill_running(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO knowledge_core_backfills (version, status, started_at, summary_json)
                VALUES (9, 'running', ?, '{}')
                ON CONFLICT(version) DO UPDATE SET
                    status = 'running', started_at = excluded.started_at,
                    completed_at = NULL, last_error = NULL
                """,
                (_now(),),
            )

    def _run_backfill_checkpoint(
        self,
        item_type: str,
        item_id: str,
        processor: Callable[[sqlite3.Connection], tuple[str, str | None]],
    ) -> bool:
        if self._backfill_item_is_completed(item_type, item_id):
            return False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                status, detail = processor(connection)
                self._record_backfill_item_tx(
                    connection,
                    item_type,
                    item_id,
                    status,
                    detail,
                    _now(),
                    detail if status != "completed" else None,
                )
            return status == "completed"
        except ClaimEvidenceValidationError as exc:
            self._record_backfill_item(
                item_type, item_id, "needs_attention", str(exc), str(exc)
            )
        except ValueError as exc:
            self._record_backfill_item(
                item_type, item_id, "needs_attention", str(exc), str(exc)
            )
        except Exception as exc:  # Keep the remaining checkpoints recoverable.
            self._record_backfill_item(item_type, item_id, "failed", str(exc), str(exc))
        return False

    def _backfill_item_is_completed(self, item_type: str, item_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT status FROM knowledge_core_backfill_items
                WHERE backfill_version = 9 AND item_kind = ? AND item_id = ?
                """,
                (item_type, item_id),
            ).fetchone()
        return row is not None and row["status"] == "completed"

    def _record_backfill_item(
        self,
        item_type: str,
        item_id: str,
        status: str,
        detail: str | None,
        error_message: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._record_backfill_item_tx(
                connection, item_type, item_id, status, detail, _now(), error_message
            )

    def _complete_backfill(self) -> BackfillSummary:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            summary = self._summary_tx(connection)
            connection.execute(
                """
                UPDATE knowledge_core_backfills
                SET status = ?, completed_at = ?, summary_json = ?, last_error = ?
                WHERE version = 9
                """,
                (
                    summary.status,
                    _now(),
                    _dump(summary.model_dump()),
                    "unresolved backfill items" if summary.status != "completed" else None,
                ),
            )
        return summary

    def _ensure_source_tx(
        self,
        connection: sqlite3.Connection,
        *,
        title: str,
        uri: str,
        source_type: str,
        metadata: dict[str, Any],
        content_sha256: str,
        now: str,
    ) -> str:
        raw_uri = uri.strip() or f"legacy://source/{_sha256(title)}"
        identity = derive_source_identity(
            uri=raw_uri, content_sha256=content_sha256, metadata=metadata
        )
        row = connection.execute(
            """
            SELECT id FROM sources
            WHERE canonical_uri = ? AND version = ?
            """,
            (identity.canonical_uri, identity.version),
        ).fetchone()
        if row is not None:
            connection.execute(
                """
                UPDATE sources
                SET title = ?, source_type = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, source_type, _dump(metadata), now, row["id"]),
            )
            return str(row["id"])
        source_id = f"core-source-{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO sources (
                id, source_type, title, uri, canonical_uri, version, content_sha256,
                metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                source_type,
                title,
                raw_uri,
                identity.canonical_uri,
                identity.version,
                identity.content_sha256,
                _dump(metadata),
                now,
                now,
            ),
        )
        return source_id

    def _upsert_chunk_tx(
        self, connection: sqlite3.Connection, chunk: DocumentChunk, now: str
    ) -> None:
        metadata = dict(chunk.metadata)
        page_start = int(metadata.get("page_start", 1) or 1)
        page_end = int(metadata.get("page_end", page_start) or page_start)
        connection.execute(
            """
            INSERT INTO chunks (
                id, document_id, legacy_chunk_id, content, content_sha256,
                chunk_index, page_start, page_end, location_json, embedding_ref,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                document_id = excluded.document_id,
                legacy_chunk_id = excluded.legacy_chunk_id,
                content = excluded.content,
                content_sha256 = excluded.content_sha256,
                chunk_index = excluded.chunk_index,
                page_start = excluded.page_start,
                page_end = excluded.page_end,
                location_json = excluded.location_json,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                chunk.id,
                chunk.paper_id,
                chunk.id,
                chunk.text,
                _sha256(chunk.text),
                chunk.chunk_index,
                page_start,
                page_end,
                _dump({"page_start": page_start, "page_end": page_end}),
                _dump(metadata),
                now,
                now,
            ),
        )

    def _ensure_claim_tx(
        self,
        connection: sqlite3.Connection,
        *,
        legacy_id: str,
        entity_id: str | None,
        relation_id: str | None,
        subject: str,
        predicate: str,
        object_value: str,
        claim_type: str,
        statement: str,
        confidence: float | None,
        properties: dict[str, Any],
        now: str,
    ) -> str:
        claim_id = self._mapped_id_tx(connection, "claims", legacy_id)
        if claim_id is None:
            claim_id = f"core-claim-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO claims (
                    id, legacy_id, entity_id, relation_id, subject, predicate, object_value,
                    claim_type, statement, confidence, status, properties_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?)
                """,
                (
                    claim_id,
                    legacy_id,
                    entity_id,
                    relation_id,
                    subject,
                    predicate,
                    object_value,
                    claim_type,
                    statement,
                    confidence,
                    _dump(properties),
                    now,
                    now,
                ),
            )
            self._insert_mapping_tx(connection, "claims", legacy_id, "claims", claim_id)
        else:
            connection.execute(
                """
                UPDATE claims
                SET entity_id = ?, relation_id = ?, subject = ?, predicate = ?, object_value = ?,
                    claim_type = ?, statement = ?, confidence = ?, status = 'published',
                    properties_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    entity_id,
                    relation_id,
                    subject,
                    predicate,
                    object_value,
                    claim_type,
                    statement,
                    confidence,
                    _dump(properties),
                    now,
                    claim_id,
                ),
            )
        return claim_id

    def _link_evidence_tx(
        self,
        connection: sqlite3.Connection,
        claim_id: str,
        evidence: EvidenceSpan,
        now: str,
    ) -> None:
        chunk_row, source_id, location = self._validate_evidence_tx(connection, evidence)
        quote = evidence.quote.strip()
        quote_hash = _sha256(quote)
        evidence_row = connection.execute(
            "SELECT id FROM evidences WHERE chunk_id = ? AND quote_sha256 = ?",
            (chunk_row["id"], quote_hash),
        ).fetchone()
        if evidence_row is None:
            evidence_id = f"core-evidence-{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO evidences (
                    id, source_id, chunk_id, quote, quote_sha256, location_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    evidence_id,
                    source_id,
                    chunk_row["id"],
                    quote,
                    quote_hash,
                    _dump(location),
                    now,
                ),
            )
        else:
            evidence_id = str(evidence_row["id"])
        connection.execute(
            """
            INSERT OR IGNORE INTO claim_evidence_links (
                claim_id, evidence_id, support_role, created_at
            )
            VALUES (?, ?, 'supports', ?)
            """,
            (claim_id, evidence_id, now),
        )

    def _validate_evidence_collection_tx(
        self, connection: sqlite3.Connection, evidence_items: list[EvidenceSpan]
    ) -> None:
        if not evidence_items:
            raise ClaimEvidenceValidationError(
                "A published Core claim requires at least one Evidence item."
            )
        for evidence in evidence_items:
            self._validate_evidence_tx(connection, evidence)

    def _validate_evidence_tx(
        self, connection: sqlite3.Connection, evidence: EvidenceSpan
    ) -> tuple[sqlite3.Row, str, dict[str, Any]]:
        chunk_row = connection.execute(
            """
            SELECT chunk.id, chunk.document_id, chunk.content, chunk.page_start, chunk.page_end,
                   document.source_id
            FROM chunks chunk
            JOIN documents document ON document.id = chunk.document_id
            WHERE chunk.id = ? OR chunk.legacy_chunk_id = ?
            """,
            (evidence.chunk_id, evidence.chunk_id),
        ).fetchone()
        if chunk_row is None:
            raise ClaimEvidenceValidationError(
                f"Evidence chunk {evidence.chunk_id!r} does not exist in SQLite Core."
            )
        if str(chunk_row["document_id"]) != evidence.paper_id:
            raise ClaimEvidenceValidationError(
                "Evidence paper_id does not match the document that owns its Chunk."
            )
        if not chunk_row["source_id"]:
            raise ClaimEvidenceValidationError(
                "Evidence Chunk belongs to a Document without a Source."
            )
        if evidence.page_end < int(chunk_row["page_start"]) or evidence.page_start > int(
            chunk_row["page_end"]
        ):
            raise ClaimEvidenceValidationError("Evidence page range does not overlap its Chunk.")

        quote = evidence.quote.strip()
        normalized_quote = _normalize_for_match(quote)
        normalized_content = _normalize_for_match(str(chunk_row["content"]))
        if normalized_quote:
            if normalized_quote not in normalized_content:
                raise ClaimEvidenceValidationError(
                    "Evidence quote cannot be located in its Chunk content."
                )
            locator = {"strategy": "quote", "quote_normalized": normalized_quote}
        else:
            locator = {
                "strategy": "page_range_fallback",
                "reason": "quote_empty",
            }
        return (
            chunk_row,
            str(chunk_row["source_id"]),
            {
                "paper_id": evidence.paper_id,
                "page_start": evidence.page_start,
                "page_end": evidence.page_end,
                "locator": locator,
            },
        )

    def _mapped_id_tx(
        self, connection: sqlite3.Connection, legacy_table: str, legacy_id: str
    ) -> str | None:
        row = connection.execute(
            "SELECT core_id FROM legacy_record_map WHERE legacy_table = ? AND legacy_id = ?",
            (legacy_table, legacy_id),
        ).fetchone()
        return str(row["core_id"]) if row else None

    def _insert_mapping_tx(
        self,
        connection: sqlite3.Connection,
        legacy_table: str,
        legacy_id: str,
        core_table: str,
        core_id: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO legacy_record_map (legacy_table, legacy_id, core_table, core_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (legacy_table, legacy_id, core_table, core_id, _now()),
        )

    def _record_backfill_item_tx(
        self,
        connection: sqlite3.Connection,
        item_type: str,
        item_id: str,
        status: str,
        detail: str | None,
        now: str,
        error_message: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO knowledge_core_backfill_items (
                backfill_version, item_kind, item_type, item_id, status, detail,
                error_message, updated_at
            ) VALUES (9, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(backfill_version, item_kind, item_id) DO UPDATE SET
                item_type = excluded.item_type, status = excluded.status, detail = excluded.detail,
                error_message = excluded.error_message, updated_at = excluded.updated_at
            """,
            (item_type, item_type, item_id, status, detail, error_message, now),
        )

    def _summary_tx(self, connection: sqlite3.Connection) -> BackfillSummary:
        def count(table: str) -> int:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        needs_attention = int(
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_core_backfill_items WHERE backfill_version = 9 "
                "AND status = 'needs_attention'"
            ).fetchone()[0]
        )
        failed = int(
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_core_backfill_items WHERE backfill_version = 9 "
                "AND status = 'failed'"
            ).fetchone()[0]
        )
        if failed:
            status = "failed"
        elif needs_attention:
            status = "completed_with_attention"
        else:
            status = "completed"
        return BackfillSummary(
            version=9,
            status=status,
            sources=count("sources"),
            documents=count("documents"),
            chunks=count("chunks"),
            entities=count("entities"),
            relations=count("relations"),
            claims=count("claims"),
            evidences=count("evidences"),
            evidence_links=count("claim_evidence_links"),
            needs_attention=needs_attention,
            failed=failed,
        )


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _core_claim_from_row(row: sqlite3.Row) -> CoreClaim:
    return CoreClaim(
        id=str(row["id"]),
        legacy_id=row["legacy_id"],
        entity_id=row["entity_id"],
        relation_id=row["relation_id"],
        subject=str(row["subject"]),
        predicate=str(row["predicate"]),
        object_value=str(row["object_value"]),
        claim_type=str(row["claim_type"]),
        statement=str(row["statement"]),
        confidence=row["confidence"],
        status=str(row["status"]),
        properties=_load_json(row["properties_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
