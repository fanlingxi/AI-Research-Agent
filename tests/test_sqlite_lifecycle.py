from __future__ import annotations

import sqlite3

import pytest

from app.persistence.sqlite import SQLiteDatabase


@pytest.mark.parametrize("fail", [False, True])
def test_repository_transaction_closes_handle_and_preserves_atomicity(tmp_path, fail) -> None:
    path = tmp_path / "transaction.db"
    database = SQLiteDatabase(str(path))
    with database.connect() as setup:
        setup.execute("CREATE TABLE facts (value TEXT NOT NULL)")

    connection = database.connect()
    try:
        with connection:
            connection.execute("INSERT INTO facts VALUES ('committed fact')")
            if fail:
                raise ValueError("abort transaction")
    except ValueError:
        assert fail

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with database.connect() as reader:
        rows = reader.execute("SELECT value FROM facts").fetchall()
    assert [row[0] for row in rows] == ([] if fail else ["committed fact"])
    # Windows refuses this unlink while a connection still owns the file.
    path.unlink()
    assert not path.exists()
