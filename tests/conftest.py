"""Session-wide test isolation for application imports and external stores."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_DATABASE = ROOT / "data" / "knowledge" / "knowledge.db"
TEST_RUNTIME_ROOT = Path(tempfile.mkdtemp(prefix="ai-research-agent-tests-"))

# This module is loaded before test modules are imported, ensuring every test
# service receives temporary stores and deterministic provider settings.
os.environ.update(
    {
        "LLM_PROVIDER": "mock",
        "KNOWLEDGE_DB_PATH": str(TEST_RUNTIME_ROOT / "knowledge.db"),
        "AGENT_CHECKPOINT_PATH": str(TEST_RUNTIME_ROOT / "agent_checkpoints.db"),
        "KNOWLEDGE_VAULT_PATH": str(TEST_RUNTIME_ROOT / "vault"),
        "XDG_CACHE_HOME": str(TEST_RUNTIME_ROOT / "cache"),
        "QDRANT_URL": "http://127.0.0.1:9",
        "NEO4J_URI": "bolt://127.0.0.1:9",
        "RUN_STORE_INTEGRATION": "0",
        "RUN_LIVE_LLM_INTEGRATION": "0",
    }
)


def database_fingerprint(path: Path = REAL_DATABASE) -> dict[str, Any]:
    """Read the real database and SQLite sidecars without opening them for write."""

    files: dict[str, dict[str, int | str] | None] = {}
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        if not candidate.exists():
            files[candidate.name] = None
            continue
        stat = candidate.stat()
        files[candidate.name] = {
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }

    schema_version: int | None = None
    if path.exists():
        uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            schema_version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
    return {"schema_version": schema_version, "files": files}


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config._real_database_baseline = database_fingerprint()  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    try:
        before = session.config._real_database_baseline  # type: ignore[attr-defined]
        after = database_fingerprint()
        if after != before:
            reporter = session.config.pluginmanager.getplugin("terminalreporter")
            if reporter is not None:
                reporter.write_line(
                    "ERROR: test run changed data/knowledge/knowledge.db or a SQLite sidecar."
                )
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    finally:
        shutil.rmtree(TEST_RUNTIME_ROOT, ignore_errors=True)
