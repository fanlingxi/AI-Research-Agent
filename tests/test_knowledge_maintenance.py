import sqlite3
from pathlib import Path

from app.knowledge.maintenance import refresh_placeholder_titles
from app.knowledge.repository import KnowledgeRepository

PDF = Path("benchmarks/research/papers-v1/papers/alce.pdf").resolve()


def test_refresh_titles_replaces_only_placeholders(tmp_path) -> None:
    db = tmp_path / "knowledge.db"
    KnowledgeRepository(str(db))
    with sqlite3.connect(db) as connection:  # raw rows; FK checks are off here
        for doc_id, title in (("doc-a", "untitled"), ("doc-b", "Kept title")):
            connection.execute(
                "INSERT INTO sources (id, source_type, title, uri, created_at, updated_at)"
                " VALUES (?, 'pdf', ?, ?, 't', 't')",
                (f"src-{doc_id}", title, f"file://{doc_id}"),
            )
            connection.execute(
                "INSERT INTO documents (id, ingestion_id, title, source, local_path, pages,"
                " metadata_json, source_id) VALUES (?, 'ing', ?, 's', ?, 1, '{}', ?)",
                (doc_id, title, str(PDF), f"src-{doc_id}"),
            )

    assert refresh_placeholder_titles(str(db), dry_run=True)[0]["document_id"] == "doc-a"
    changes = refresh_placeholder_titles(str(db))

    with sqlite3.connect(db) as connection:
        titles = dict(connection.execute("SELECT id, title FROM documents"))
        source = connection.execute("SELECT title FROM sources WHERE id = 'src-doc-a'").fetchone()
    assert [c["document_id"] for c in changes] == ["doc-a"]
    assert titles["doc-a"].startswith("Enabling Large Language Models")
    assert source[0] == titles["doc-a"]
    assert titles["doc-b"] == "Kept title"
    assert refresh_placeholder_titles(str(db)) == []
