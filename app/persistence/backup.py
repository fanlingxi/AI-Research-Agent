"""Verified single-database snapshots; restores always create a new directory.

Stop API/Workers before pairing knowledge and checkpoint backups. Separate online
backups are individually consistent, not an atomic snapshot across databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup_database(source: Path, directory: Path) -> dict:
    source, directory = Path(source).resolve(strict=True), Path(directory).resolve()
    if not source.is_file():
        raise ValueError("Source must be an existing SQLite database")
    directory.mkdir(parents=True, exist_ok=False)
    target = directory / "database.db"
    deadline = time.monotonic() + 60

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("Backup exceeded 60 seconds; incomplete directory retained")

    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer, pages=256, progress=progress, sleep=0.1)
            check = [row[0] for row in writer.execute("PRAGMA integrity_check")]
            if check != ["ok"]:
                raise ValueError("Backup integrity check failed")
    result = {
        "schema": "sqlite-backup-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "source": str(source),
        "database": "database.db",
        "sha256": _digest(target),
        "bytes": target.stat().st_size,
        "integrity_check": "ok",
        "scope": "single database, including committed WAL; not cross-database atomic",
    }
    (directory / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def restore_database(archive: Path, directory: Path) -> dict:
    archive = Path(archive).resolve(strict=True)
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    source = archive / "database.db"
    if (
        manifest.get("schema") != "sqlite-backup-v1"
        or manifest.get("database") != "database.db"
        or source.is_symlink()
        or _digest(source) != manifest.get("sha256")
    ):
        raise ValueError("Backup identity/checksum mismatch; restore refused")
    return backup_database(source, directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("source", type=Path)
    parser.add_argument("new_directory", type=Path)
    args = parser.parse_args()
    operation = backup_database if args.action == "backup" else restore_database
    print(json.dumps(operation(args.source, args.new_directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
