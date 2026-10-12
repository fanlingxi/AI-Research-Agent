"""Small, explicit maintenance commands for an existing knowledge database.

Usage:
    python -m app.knowledge.maintenance refresh-titles [--db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from app.config.settings import get_settings
from app.persistence.sqlite import SQLiteDatabase
from app.tools.pdf_tools import _usable_title, parse_pdf_source


def refresh_placeholder_titles(db_path: str, *, dry_run: bool = False) -> list[dict[str, str]]:
    """Re-derive titles for documents whose stored title is a placeholder such as "untitled".

    Only the display title of the document and its source changes; content, hashes,
    source versions and existing snapshots stay untouched. PDFs are parsed outside any
    transaction and each update re-checks that the title is still the placeholder.
    """

    database = SQLiteDatabase(db_path)
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT id, title, local_path, source_id FROM documents"
        ).fetchall()
    stale = [row for row in rows if _usable_title(str(row["title"] or "")) is None]
    changes = []
    for row in stale:
        path = Path(str(row["local_path"] or ""))
        if path.suffix.lower() != ".pdf" or not path.is_file():
            continue
        title = parse_pdf_source(str(path), max_pages=1).title
        if _usable_title(title) is None or title == path.stem:
            continue
        changes.append({"document_id": str(row["id"]), "old": str(row["title"]), "new": title})
        if dry_run:
            continue
        now = datetime.now(tz=UTC).isoformat()
        with database.connect() as connection:
            updated = connection.execute(
                "UPDATE documents SET title = ? WHERE id = ? AND title = ?",
                (title, row["id"], row["title"]),
            ).rowcount
            if updated and row["source_id"]:
                connection.execute(
                    "UPDATE sources SET title = ?, updated_at = ? WHERE id = ? AND title = ?",
                    (title, now, row["source_id"], row["title"]),
                )
            connection.commit()
    return changes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    titles = sub.add_parser("refresh-titles", help="replace placeholder titles from the PDFs")
    titles.add_argument("--db", default=None, help="knowledge DB (default: settings)")
    titles.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db or get_settings().knowledge_db_path
    changes = refresh_placeholder_titles(db_path, dry_run=args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "changes": changes}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
