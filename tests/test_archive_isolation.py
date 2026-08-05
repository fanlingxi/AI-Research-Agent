from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v1_archive_is_excluded_from_active_tooling_and_docker_context() -> None:
    assert "archive" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "archive/" in (ROOT / ".rgignore").read_text(encoding="utf-8").splitlines()
    assert 'exclude = ["archive"]' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "archive/*" in (ROOT / ".coveragerc").read_text(encoding="utf-8")


def test_active_tree_has_no_v1_runtime_packages_or_archive_imports() -> None:
    removed = [
        "agents",
        "evaluation",
        "evidence",
        "graph",
        "graphrag",
        "memory",
        "obsidian",
    ]
    assert all(not list((ROOT / "app" / package).glob("*.py")) for package in removed)
    active_python = list((ROOT / "app").rglob("*.py")) + [ROOT / "main.py"]
    contents = "\n".join(path.read_text(encoding="utf-8") for path in active_python)
    assert "from archive" not in contents
    assert "import archive" not in contents
