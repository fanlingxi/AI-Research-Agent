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
    if not dry_run:
        _repair_templated_statements(database)
    return changes


def _repair_templated_statements(database: SQLiteDatabase) -> None:
    """Swap the placeholder book title in generated "《untitled》提出了…" summaries.

    Only the quoted title token changes, and only when the claim's own evidence (or the
    relation's evidence papers) resolves to exactly one real title.
    """

    with database.connect() as connection:
        claims = connection.execute(
            "SELECT id, statement FROM claims WHERE statement LIKE '《%》%'"
        ).fetchall()
        for claim in claims:
            token = _placeholder_token(str(claim["statement"]))
            if not token:
                continue
            titles = {
                str(row["title"])
                for row in connection.execute(
                    "SELECT s.title FROM claim_evidence_links l"
                    " JOIN evidences e ON e.id = l.evidence_id"
                    " JOIN sources s ON s.id = e.source_id WHERE l.claim_id = ?",
                    (claim["id"],),
                )
            }
            if len(titles) == 1 and _usable_title(next(iter(titles))):
                connection.execute(
                    "UPDATE claims SET statement = ? WHERE id = ?",
                    (str(claim["statement"]).replace(token, f"《{titles.pop()}》", 1), claim["id"]),
                )
        relations = connection.execute(
            "SELECT id, payload_json FROM published_relations WHERE payload_json LIKE '%《%》%'"
        ).fetchall()
        for relation in relations:
            payload = json.loads(relation["payload_json"])
            token = _placeholder_token(str(payload.get("summary") or ""))
            papers = {e.get("paper_id") for e in payload.get("evidence") or []} - {None}
            if not token or len(papers) != 1:
                continue
            row = connection.execute(
                "SELECT title FROM documents WHERE id = ?", (papers.pop(),)
            ).fetchone()
            if row and _usable_title(str(row["title"])):
                payload["summary"] = payload["summary"].replace(token, f"《{row['title']}》", 1)
                connection.execute(
                    "UPDATE published_relations SET payload_json = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), relation["id"]),
                )
        connection.commit()


def _placeholder_token(statement: str) -> str | None:
    if not statement.startswith("《") or "》" not in statement:
        return None
    token = statement[: statement.index("》") + 1]
    return token if _usable_title(token[1:-1]) is None else None


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
