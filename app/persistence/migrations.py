from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIRECTORY = Path(__file__).with_name("migrations")


def sql_migration(version: int) -> str:
    """Load a numbered structural migration from the package resources.

    Data backfills deliberately do not go through this loader.  They are explicit,
    resumable operations because they can depend on an external projection such as
    Qdrant and must never run as an application-start side effect.
    """

    matching = sorted(MIGRATIONS_DIRECTORY.glob(f"{version:04d}_*.sql"))
    if len(matching) != 1:
        raise RuntimeError(f"Expected exactly one SQL migration for version {version}.")
    return matching[0].read_text(encoding="utf-8")


def apply_structural_migration(
    connection: sqlite3.Connection,
    *,
    version: int,
    sql: str,
    applied_at: str,
) -> None:
    """Apply an additive SQL migration and record its version atomically.

    Existing v1-v7 migrations retain their historical executor. New structural
    migrations use this explicit transaction so their schema change and migration
    record cannot be separated by a normal application restart.
    """

    applied = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    if applied:
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in _statements(sql):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, applied_at),
        )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _statements(sql: str) -> list[str]:
    """Return statements for the checked-in migration format.

    Migration files must keep SQL literals free of semicolons; this makes the
    parser deliberately small and keeps reviews of structural migrations clear.
    """

    return [statement.strip() for statement in sql.split(";") if statement.strip()]
