import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.persistence.migrations import apply_structural_migration


def test_structural_migration_rechecks_version_after_waiting_for_write_lock(tmp_path) -> None:
    database = tmp_path / "migration-race.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE documents (id TEXT PRIMARY KEY)")

    barrier = Barrier(2)

    def apply_from_separate_process() -> None:
        connection = sqlite3.connect(database, timeout=2, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout = 2000")
        barrier.wait()
        apply_structural_migration(
            connection,
            version=99,
            sql="ALTER TABLE documents ADD COLUMN source_id TEXT",
            applied_at="2026-08-19T00:00:00+00:00",
        )
        connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply_from_separate_process) for _ in range(2)]
        for future in futures:
            future.result()

    with sqlite3.connect(database) as connection:
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 99"
        ).fetchone()[0]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}

    assert migration_count == 1
    assert "source_id" in columns
