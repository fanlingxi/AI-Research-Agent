import json
import sqlite3
from contextlib import closing

import pytest

from app.persistence.backup import backup_database, restore_database


def test_backup_includes_committed_wal_and_restore_never_overwrites(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE facts (value TEXT)")
        writer.execute("INSERT INTO facts VALUES ('committed')")
        writer.commit()
        writer.execute("INSERT INTO facts VALUES ('uncommitted')")
        archive = tmp_path / "backup"
        result = backup_database(source, archive)
        assert result["integrity_check"] == "ok"
        restored = tmp_path / "restored"
        restore_database(archive, restored)
        with closing(sqlite3.connect(restored / "database.db")) as reader:
            assert reader.execute("SELECT value FROM facts").fetchall() == [("committed",)]
        before = source.read_bytes()
        with pytest.raises(FileExistsError):
            restore_database(archive, tmp_path)
        assert source.read_bytes() == before
        writer.rollback()


def test_tampered_or_incomplete_backup_is_not_restored(tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.execute("CREATE TABLE facts (value TEXT)")
    archive = tmp_path / "archive"
    backup_database(source, archive)
    manifest = archive / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sha256"] = "wrong"
    manifest.write_text(json.dumps(value))
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="checksum"):
        restore_database(archive, destination)
    assert not destination.exists()
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "missing.db", destination)
    assert not destination.exists()
