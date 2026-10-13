"""Version lifecycle metadata; never rewrites source content or frozen artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.persistence.sqlite import SQLiteDatabase

PUBLISHED_SOURCE_SQL = """
    SELECT DISTINCT ev.source_id
    FROM evidences ev
    JOIN claim_evidence_links link ON link.evidence_id = ev.id
    JOIN claims claim ON claim.id = link.claim_id AND claim.status = 'published'
    LEFT JOIN entities entity ON entity.id = claim.entity_id
    LEFT JOIN relations relation ON relation.id = claim.relation_id
    WHERE (entity.status = 'published' OR relation.status = 'published')
"""


REPLACED_SOURCE_SQL = f"""
    SELECT id FROM sources WHERE canonical_uri = ? AND id != ?
    AND id IN ({PUBLISHED_SOURCE_SQL})
"""


class SourceVersionRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def activation_preview(self, source_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            source = connection.execute(
                "SELECT canonical_uri FROM sources WHERE id = ?", (source_id,),
            ).fetchone()
            if source is None:
                raise KeyError(source_id)
            replaced = {row[0] for row in connection.execute(
                REPLACED_SOURCE_SQL, (source["canonical_uri"], source_id),
            )}
            result = {"source_id": source_id, "replaced_version_count": len(replaced)}
            for kind, table, label, joins in (
                ("entity", "entities", "item.name", ""),
                ("relation", "relations",
                 "left_entity.name || ' — ' || item.relation_type || ' → ' || right_entity.name",
                 "JOIN entities left_entity ON left_entity.id = item.source_entity_id "
                 "JOIN entities right_entity ON right_entity.id = item.target_entity_id"),
            ):
                rows = connection.execute(f"""
                    SELECT item.id, {label} AS name, ev.source_id, s.superseded_by_source_id
                    FROM {table} item {joins}
                    JOIN claims c ON c.{kind}_id = item.id AND c.status = 'published'
                    JOIN claim_evidence_links link ON link.claim_id = c.id
                    JOIN evidences ev ON ev.id = link.evidence_id
                    JOIN sources s ON s.id = ev.source_id
                    WHERE item.status = 'published'
                """).fetchall()
                current, surviving, names = set(), set(), {}
                for row in rows:
                    item_id = row["id"]
                    names[item_id] = row["name"]
                    if row["superseded_by_source_id"] is None:
                        current.add(item_id)
                        if row["source_id"] not in replaced:
                            surviving.add(item_id)
                    if row["source_id"] == source_id:
                        surviving.add(item_id)
                lost = sorted(current - surviving, key=lambda item_id: (names[item_id], item_id))
                result[f"{kind}_count"] = len(lost)
                result[f"{kind}_samples"] = [names[item_id] for item_id in lost[:20]]
        return result

    def activate(self, source_id: str) -> dict:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                "SELECT canonical_uri FROM sources WHERE id = ?", (source_id,),
            ).fetchone()
            if source is None:
                raise KeyError(source_id)
            if not source["canonical_uri"]:
                raise ValueError("该版本缺少来源标识，无法启用。")
            published = {row[0] for row in connection.execute(PUBLISHED_SOURCE_SQL)}
            if source_id not in published:
                raise ValueError("该版本尚无已审核发布的知识证据，请先审核候选知识。")
            now = datetime.now(UTC).isoformat()
            connection.execute(
                f"""UPDATE sources SET superseded_by_source_id = ?, superseded_at = ?
                    WHERE id IN ({REPLACED_SOURCE_SQL})""",
                (source_id, now, source["canonical_uri"], source_id),
            )
            connection.execute(
                "UPDATE sources SET superseded_by_source_id = NULL, superseded_at = NULL "
                "WHERE id = ?", (source_id,),
            )
        return {"source_id": source_id, "status": "active"}

    def documents(self, ingestion_id: str | None = None) -> list[dict]:
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                """SELECT d.id AS document_id, d.title, d.local_path, d.parser_version,
                          d.ingestion_id, s.id AS source_id, s.canonical_uri, s.version,
                          s.superseded_by_source_id, s.superseded_at, s.created_at
                   FROM documents d JOIN sources s ON s.id = d.source_id
                   ORDER BY s.created_at DESC, d.id""",
            ).fetchall()
            published = {row[0] for row in connection.execute(PUBLISHED_SOURCE_SQL)}
            memberships: dict[str, list[str]] = {}
            for row in connection.execute(
                "SELECT document_id, ingestion_id FROM ingestion_documents"
            ):
                memberships.setdefault(row[0], []).append(row[1])
            selected = None if ingestion_id is None else {
                row[0] for row in connection.execute(
                    "SELECT document_id FROM ingestion_documents WHERE ingestion_id = ?",
                    (ingestion_id,),
                )
            }
        versions: dict[str, list[dict]] = {}
        for row in rows:
            item = dict(row)
            item["ingestion_ids"] = memberships.get(item["document_id"], [])
            item["has_published_evidence"] = item["source_id"] in published
            item["is_active"] = (
                item["has_published_evidence"] and item["superseded_by_source_id"] is None
            )
            versions.setdefault(item["canonical_uri"], []).append(item)
        result = []
        for family in versions.values():
            for item in family:
                if selected is not None and item["document_id"] not in selected:
                    continue
                path = Path(item["local_path"] or "")
                result.append({
                    **item, "can_reparse": path.suffix.lower() == ".pdf" and path.is_file(),
                    "versions": [{key: value for key, value in version.items()
                                  if key not in {"local_path", "canonical_uri"}}
                                 for version in family],
                })
        return result
