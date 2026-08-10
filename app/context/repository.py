"""Persistence for immutable Context Builder audit snapshots only."""

from __future__ import annotations

import hmac
import json
from typing import Any

from app.context.models import (
    ContextPackage,
    ContextSnapshotItem,
    canonical_package_sha256,
)
from app.persistence.sqlite import SQLiteDatabase


class ContextSnapshotRepository:
    """Append-only storage that never mutates Knowledge or Memory records."""

    def __init__(self, path: str, database: SQLiteDatabase | None = None) -> None:
        self.path = path
        self.database = database or SQLiteDatabase(path)

    def save(self, package: ContextPackage, items: list[ContextSnapshotItem]) -> None:
        payload = package.model_dump(mode="json")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO context_snapshots (
                    id, project_id, task_id, project_revision, task_revision,
                    builder_version, package_schema_version, request_fingerprint,
                    package_json, package_sha256, token_budget, used_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package.snapshot_id,
                    package.project.project_id,
                    package.task.task_id,
                    package.project.revision,
                    package.task.revision,
                    package.builder_version,
                    package.package_schema_version,
                    package.request_fingerprint,
                    _dump(payload),
                    package.package_sha256,
                    package.token_usage.budget,
                    package.token_usage.used,
                    package.created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO context_snapshot_items (
                    snapshot_id, section, item_type, item_id, parent_item_id, rank,
                    score, selected_reason, provenance_json, estimated_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        package.snapshot_id,
                        item.section,
                        item.item_type,
                        item.item_id,
                        item.parent_item_id,
                        item.rank,
                        item.score,
                        item.selected_reason,
                        _dump(item.provenance),
                        item.estimated_tokens,
                    )
                    for item in items
                ],
            )

    def get(self, snapshot_id: str) -> ContextPackage:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT package_json, package_sha256 FROM context_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"ContextSnapshot {snapshot_id} not found")
        try:
            package = ContextPackage.model_validate_json(row["package_json"])
        except ValueError as exc:
            raise SnapshotIntegrityError(
                f"ContextSnapshot {snapshot_id} has an invalid stored package."
            ) from exc
        actual = canonical_package_sha256(package)
        stored = str(row["package_sha256"])
        if not (
            hmac.compare_digest(actual, stored)
            and hmac.compare_digest(package.package_sha256, stored)
        ):
            raise SnapshotIntegrityError(
                f"ContextSnapshot {snapshot_id} failed its package SHA-256 integrity check."
            )
        return package

    def list_items(self, snapshot_id: str) -> list[ContextSnapshotItem]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT section, item_type, item_id, parent_item_id, rank, score,
                       selected_reason, provenance_json, estimated_tokens
                FROM context_snapshot_items
                WHERE snapshot_id = ?
                ORDER BY section, rank, item_type, item_id
                """,
                (snapshot_id,),
            ).fetchall()
        return [
            ContextSnapshotItem(
                section=row["section"],
                item_type=row["item_type"],
                item_id=row["item_id"],
                parent_item_id=row["parent_item_id"],
                rank=row["rank"],
                score=row["score"],
                selected_reason=row["selected_reason"],
                provenance=json.loads(row["provenance_json"] or "{}"),
                estimated_tokens=row["estimated_tokens"],
            )
            for row in rows
        ]


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SnapshotIntegrityError(ValueError):
    """Persisted Context package JSON no longer matches its audit digest."""
