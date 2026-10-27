"""Shared formats, fixed development corpus and durable attempt budgets."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

DEFAULT_ROOT = Path(r"D:\AI-Research-Agent\data\evaluation\parser-trial\dev")
PROJECT = Path(r"D:\AI-Research-Agent")
MINERU_PYTHON = PROJECT / "data/tools/parsers/mineru/.venv/Scripts/python.exe"
PADDLE_PYTHON = PROJECT / "data/tools/parsers/paddleocr-vl/.venv/Scripts/python.exe"
ZH_PAPERS = (
    "2024.ccl-1.2",
    "2024.ccl-1.3",
    "2024.ccl-2.6",
    "2024.ccl-2.9",
    "2025.ccl-1.12",
    "2025.ccl-1.13",
    "2025.ccl-1.31",
    "2025.ccl-1.5",
)
EN_PAPERS = ("attrscore", "cove", "csqe", "e5-mistral")
PARSERS = ("mineru", "paddle", "pypdf-v3")
PROVENANCE = "开发集，只用于选候选；AI 共识标签，非人工"


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(path)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def exclusive(root: Path, name: str):
    """No simultaneous GPU runs / ledger writers; a crash leaves an inspectable lock."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / f".{name}.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write("Active trial operation. After a crash, verify no process remains.\n")
    try:
        yield
    finally:
        lock.unlink()


def reserve(root: Path, name: str, key: str, units: int, limit: int) -> tuple[Path, dict]:
    """Reserve before starting; failures and interrupted attempts still consume budget.

    Caller holds exclusive(). Existing attempts are never automatically retried.
    """
    path = root / "attempts" / f"{name}.json"
    ledger = read(path) if path.exists() else {}
    if key in ledger:
        raise ValueError(f"Attempt already recorded: {name}/{key}; no automatic retry")
    if units < 1 or sum(r["units"] for r in ledger.values()) + units > limit:
        raise ValueError(f"{name} cumulative budget exceeds {limit}")
    ledger[key] = {"units": units, "status": "started"}
    write(path, ledger)
    return path, ledger
