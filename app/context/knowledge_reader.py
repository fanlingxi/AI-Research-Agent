"""SQLite-authoritative formal Knowledge reads for Context Builder."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawEvidence:
    evidence: dict[str, Any]
    chunk: dict[str, Any]
    document: dict[str, Any]
    source: dict[str, Any]


@dataclass
class RawKnowledgeBundle:
    claim: dict[str, Any]
    entity: dict[str, Any] | None
    relation: dict[str, Any] | None
    collection_scopes: list[str]
    evidence: list[RawEvidence] = field(default_factory=list)


class KnowledgeContextReader:
    """Read only published, evidence-grounded Core claims from one connection.

    A vector or graph projection is never trusted as a fact source. This reader
    is the revalidation boundary and is deliberately limited to SQLite rows.
    Entity and Relation visibility each follows that aggregate's independent
    ``collection_memberships`` record, matching the legacy report scope rules;
    Relation endpoint membership is not treated as an implicit authorization.
    """

    def read_formal_bundles_tx(
        self, connection: sqlite3.Connection, collection_scopes: list[str]
    ) -> list[RawKnowledgeBundle]:
        if not collection_scopes:
            return []

        placeholders = ", ".join("?" for _ in collection_scopes)
        scope_filter = f"""
            EXISTS (
                SELECT 1
                FROM collection_memberships membership
                JOIN legacy_record_map mapping
                  ON mapping.legacy_id = membership.aggregate_id
                WHERE membership.collection_slug IN ({placeholders})
                  AND (
                    (
                      eligible_claims.entity_id IS NOT NULL
                      AND membership.aggregate_type = 'entity'
                      AND mapping.legacy_table = 'published_entities'
                      AND mapping.core_table = 'entities'
                      AND mapping.core_id = eligible_claims.entity_id
                    )
                    OR (
                      eligible_claims.relation_id IS NOT NULL
                      AND membership.aggregate_type = 'relation'
                      AND mapping.legacy_table = 'published_relations'
                      AND mapping.core_table = 'relations'
                      AND mapping.core_id = eligible_claims.relation_id
                    )
                  )
            )
        """
        rows = connection.execute(
            f"""
            WITH eligible_claims AS (
                SELECT
                    claim.id AS claim_id,
                    claim.legacy_id AS claim_legacy_id,
                    claim.entity_id AS claim_entity_id,
                    claim.relation_id AS claim_relation_id,
                    claim.subject AS claim_subject,
                    claim.predicate AS claim_predicate,
                    claim.object_value AS claim_object_value,
                    claim.claim_type AS claim_type,
                    claim.statement AS claim_statement,
                    claim.confidence AS claim_confidence,
                    claim.status AS claim_status,
                    claim.created_at AS claim_created_at,
                    claim.updated_at AS claim_updated_at,
                    entity.id AS entity_id,
                    entity.legacy_id AS entity_legacy_id,
                    entity.name AS entity_name,
                    entity.normalized_name AS entity_normalized_name,
                    entity.entity_type AS entity_type,
                    entity.domain AS entity_domain,
                    entity.status AS entity_status,
                    entity.created_at AS entity_created_at,
                    entity.updated_at AS entity_updated_at,
                    relation.id AS relation_id,
                    relation.legacy_id AS relation_legacy_id,
                    relation.source_entity_id AS relation_source_entity_id,
                    relation.target_entity_id AS relation_target_entity_id,
                    relation.relation_type AS relation_type,
                    relation.domain AS relation_domain,
                    relation.status AS relation_status,
                    relation.created_at AS relation_created_at,
                    relation.updated_at AS relation_updated_at
                FROM claims claim
                LEFT JOIN entities entity ON entity.id = claim.entity_id
                LEFT JOIN relations relation ON relation.id = claim.relation_id
                WHERE claim.status = 'published'
                  AND (
                    (entity.id IS NOT NULL AND entity.status = 'published')
                    OR (relation.id IS NOT NULL AND relation.status = 'published')
                  )
            )
            SELECT
                eligible_claims.*,
                evidence.id AS evidence_id,
                evidence.source_id AS evidence_source_id,
                evidence.chunk_id AS evidence_chunk_id,
                evidence.quote AS evidence_quote,
                evidence.quote_sha256 AS evidence_quote_sha256,
                evidence.location_json AS evidence_location_json,
                evidence.created_at AS evidence_created_at,
                chunk.id AS chunk_id,
                chunk.document_id AS chunk_document_id,
                chunk.chunk_index AS chunk_index,
                chunk.page_start AS chunk_page_start,
                chunk.page_end AS chunk_page_end,
                chunk.content AS chunk_content,
                chunk.content_sha256 AS chunk_content_sha256,
                chunk.location_json AS chunk_location_json,
                document.id AS document_id,
                document.source_id AS document_source_id,
                document.title AS document_title,
                document.source AS document_source,
                document.pages AS document_pages,
                document.content_sha256 AS document_content_sha256,
                document.content_status AS document_content_status,
                document.parser_version AS document_parser_version,
                document.parsed_at AS document_parsed_at,
                source.id AS source_id,
                source.source_type AS source_type,
                source.title AS source_title,
                source.uri AS source_uri,
                source.canonical_uri AS source_canonical_uri,
                source.version AS source_version,
                source.content_sha256 AS source_content_sha256,
                source.created_at AS source_created_at,
                source.updated_at AS source_updated_at
            FROM eligible_claims
            JOIN claim_evidence_links link ON link.claim_id = eligible_claims.claim_id
            JOIN evidences evidence ON evidence.id = link.evidence_id
            JOIN chunks chunk ON chunk.id = evidence.chunk_id
            JOIN documents document ON document.id = chunk.document_id
            JOIN sources source ON source.id = evidence.source_id
            WHERE source.id = document.source_id
              AND document.content_status IN ('available', 'qdrant_backfilled')
              AND {scope_filter}
            ORDER BY eligible_claims.claim_id, evidence.id
            """,
            tuple(collection_scopes),
        ).fetchall()

        bundles: dict[str, RawKnowledgeBundle] = {}
        for row in rows:
            raw = dict(row)
            evidence = self._evidence_from_row(raw)
            if evidence is None:
                continue
            claim_id = str(raw["claim_id"])
            bundle = bundles.get(claim_id)
            if bundle is None:
                owner_type, owner_legacy_id = _owner_mapping(raw)
                membership_scopes = self._membership_scopes_tx(
                    connection,
                    aggregate_type=owner_type,
                    legacy_id=owner_legacy_id,
                )
                # SQL is the authorization boundary. Keep this defensive
                # check for callers that reuse these rows in the future.
                authorized_scopes = [
                    scope for scope in membership_scopes if scope in collection_scopes
                ]
                if not authorized_scopes:
                    continue
                entity = self._entity_from_row(raw, authorized_scopes)
                relation = self._relation_from_row(raw, authorized_scopes)
                bundle = RawKnowledgeBundle(
                    claim={
                        "id": claim_id,
                        "legacy_id": raw["claim_legacy_id"],
                        "entity_id": raw["claim_entity_id"],
                        "relation_id": raw["claim_relation_id"],
                        "subject": raw["claim_subject"],
                        "predicate": raw["claim_predicate"],
                        "object_value": raw["claim_object_value"],
                        "claim_type": raw["claim_type"],
                        "statement": raw["claim_statement"],
                        "confidence": raw["claim_confidence"],
                        "status": raw["claim_status"],
                        "created_at": raw["claim_created_at"],
                        "updated_at": raw["claim_updated_at"],
                    },
                    entity=entity,
                    relation=relation,
                    collection_scopes=authorized_scopes,
                )
                bundles[claim_id] = bundle
            bundle.evidence.append(evidence)
        return list(bundles.values())

    @staticmethod
    def _membership_scopes_tx(
        connection: sqlite3.Connection, *, aggregate_type: str, legacy_id: str
    ) -> list[str]:
        """Return the authoritative current Collection membership for one owner.

        ``properties_json.collection_slugs`` is deliberately not consulted for
        authorization. It is a compatibility projection which can lag an
        ingestion move. A missing mapping or membership therefore returns an
        empty scope list and the caller has already failed closed in SQL.
        """

        rows = connection.execute(
            """
            SELECT DISTINCT collection_slug
            FROM collection_memberships
            WHERE aggregate_type = ? AND aggregate_id = ?
            ORDER BY collection_slug
            """,
            (aggregate_type, legacy_id),
        ).fetchall()
        return [str(row["collection_slug"]) for row in rows]

    @staticmethod
    def _entity_from_row(
        row: dict[str, Any], collection_scopes: list[str]
    ) -> dict[str, Any] | None:
        if row["entity_id"] is None:
            return None
        return {
            "id": row["entity_id"],
            "legacy_id": row["entity_legacy_id"],
            "name": row["entity_name"],
            "normalized_name": row["entity_normalized_name"],
            "entity_type": row["entity_type"],
            "domain": row["entity_domain"],
            "status": row["entity_status"],
            "collection_scopes": collection_scopes,
            "created_at": row["entity_created_at"],
            "updated_at": row["entity_updated_at"],
        }

    @staticmethod
    def _relation_from_row(
        row: dict[str, Any], collection_scopes: list[str]
    ) -> dict[str, Any] | None:
        if row["relation_id"] is None:
            return None
        return {
            "id": row["relation_id"],
            "legacy_id": row["relation_legacy_id"],
            "source_entity_id": row["relation_source_entity_id"],
            "target_entity_id": row["relation_target_entity_id"],
            "relation_type": row["relation_type"],
            "domain": row["relation_domain"],
            "status": row["relation_status"],
            "collection_scopes": collection_scopes,
            "created_at": row["relation_created_at"],
            "updated_at": row["relation_updated_at"],
        }

    @staticmethod
    def _evidence_from_row(row: dict[str, Any]) -> RawEvidence | None:
        quote = str(row["evidence_quote"] or "").strip()
        if (
            not quote
            or hashlib.sha256(quote.encode("utf-8")).hexdigest() != row["evidence_quote_sha256"]
        ):
            return None
        location = _load_json(row["evidence_location_json"])
        chunk_location = _load_json(row["chunk_location_json"])
        if not _is_locatable_evidence(row, quote, location):
            return None
        return RawEvidence(
            evidence={
                "id": row["evidence_id"],
                "source_id": row["evidence_source_id"],
                "chunk_id": row["evidence_chunk_id"],
                "quote": quote,
                "quote_sha256": row["evidence_quote_sha256"],
                "location": location,
                "created_at": row["evidence_created_at"],
            },
            chunk={
                "id": row["chunk_id"],
                "document_id": row["chunk_document_id"],
                "chunk_index": row["chunk_index"],
                "page_start": row["chunk_page_start"],
                "page_end": row["chunk_page_end"],
                "content": row["chunk_content"],
                "content_sha256": row["chunk_content_sha256"],
                "location": chunk_location,
            },
            document={
                "id": row["document_id"],
                "source_id": row["document_source_id"],
                "title": row["document_title"],
                "source": row["document_source"],
                "pages": row["document_pages"],
                "content_sha256": row["document_content_sha256"],
                "content_status": row["document_content_status"],
                "parser_version": row["document_parser_version"],
                "parsed_at": row["document_parsed_at"],
            },
            source={
                "id": row["source_id"],
                "source_type": row["source_type"],
                "title": row["source_title"],
                "uri": row["source_uri"],
                "canonical_uri": row["source_canonical_uri"],
                "version": row["source_version"],
                "content_sha256": row["source_content_sha256"],
                "created_at": row["source_created_at"],
                "updated_at": row["source_updated_at"],
            },
        )


def query_terms(query: str) -> list[str]:
    terms = re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query.casefold())
    return list(dict.fromkeys(terms))[:12]


def _owner_mapping(row: dict[str, Any]) -> tuple[str, str]:
    if row["entity_id"] is not None and row["entity_legacy_id"]:
        return "entity", str(row["entity_legacy_id"])
    if row["relation_id"] is not None and row["relation_legacy_id"]:
        return "relation", str(row["relation_legacy_id"])
    raise ValueError("Formal Context owner is missing its legacy Collection mapping.")


def _is_locatable_evidence(row: dict[str, Any], quote: str, location: dict[str, Any]) -> bool:
    if not row["evidence_source_id"] or not row["evidence_chunk_id"]:
        return False
    if not row["source_version"] or not row["source_content_sha256"]:
        return False
    if not row["document_content_sha256"] or not row["chunk_content_sha256"]:
        return False
    if str(location.get("paper_id") or "") != str(row["document_id"]):
        return False
    try:
        page_start = int(location["page_start"])
        page_end = int(location["page_end"])
        chunk_start = int(row["chunk_page_start"])
        chunk_end = int(row["chunk_page_end"])
    except (KeyError, TypeError, ValueError):
        return False
    if page_start < 1 or page_end < page_start or page_end < chunk_start or page_start > chunk_end:
        return False
    if not isinstance(location.get("locator"), dict):
        return False
    return _normalize_for_match(quote) in _normalize_for_match(str(row["chunk_content"] or ""))


def _load_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()
