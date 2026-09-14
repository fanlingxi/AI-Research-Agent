"""A01 data integrity boundaries, independent of model calls and production stores."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.validate_research_dataset import validate_dataset

DATASET = Path(__file__).resolve().parents[1] / "benchmarks/research/v1"


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "dataset"
    shutil.copytree(DATASET, root)
    return root


def mutate(root, filename, change):
    path = root / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_frozen_dataset_is_structurally_valid_but_not_human_approved():
    result = validate_dataset(DATASET)
    assert result["splits"] == {"dev": 25, "holdout": 15}
    assert result["annotation_status"] == {"pending_human": 40}
    assert result["model_calls"] == 0
    with pytest.raises(ValueError, match="pending human review"):
        validate_dataset(DATASET, require_human_reviewed=True)


@pytest.mark.parametrize(
    ("filename", "change", "message"),
    [
        ("corpus.json", lambda d: d["sources"][0].update(text="changed"), "text hash"),
        ("corpus.json", lambda d: d["spans"][0].update(start=0), "quote mismatch"),
        ("corpus.json", lambda d: d["spans"][0].update(end=99999), "invalid offset"),
        ("corpus.json", lambda d: d["spans"][0].update(source_version="v0"), "source_version"),
        ("corpus.json", lambda d: d["spans"][0].update(source_id="absent"), "unknown source"),
        ("tasks.dev.json", lambda d: d["tasks"][0].update(split="holdout"), "split counts"),
        ("tasks.dev.json", lambda d: d["tasks"][0].update(family_id="boreal"), "family task"),
        ("tasks.dev.json", lambda d: d["tasks"][1].update(id="atlas-q1"), "duplicate"),
        ("tasks.dev.json", lambda d: d["tasks"][0].update(allowed_source_ids=[]), "allowed"),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0].update(allowed_source_ids=["atlas-ops"]),
            "evidence outside allowed scope",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0].update(forbidden_source_ids=["atlas-spec"]),
            "overlapping scope",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0]["allowed_source_ids"].append("fjord-spec"),
            "cross-family source leakage",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][3]["expectation"].update(conflict_span_ids=[]),
            "conflict needs both sides",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0]["expectation"].update(required_evidence_span_ids=[]),
            "conclusion/evidence mismatch",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0]["annotation"].update(status="human_approved"),
            "human approval requires",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0].update(inject_answer_into_prompt=True),
            "Extra inputs",
        ),
    ],
)
def test_invalid_data_is_rejected_before_any_execution(dataset, filename, change, message):
    mutate(dataset, filename, change)
    with pytest.raises(ValueError, match=message):
        validate_dataset(dataset, verify_freeze=False)


def test_family_cannot_be_reassigned_to_holdout(dataset):
    mutate(dataset, "manifest.json", lambda d: d["families"][0].update(split="holdout"))
    with pytest.raises(ValueError, match="family split leakage"):
        validate_dataset(dataset, verify_freeze=False)


def test_semantically_plausible_edit_still_breaks_freeze(dataset):
    mutate(dataset, "tasks.dev.json", lambda d: d["tasks"][0].update(question="请说明容量。"))
    with pytest.raises(ValueError, match="frozen file hash mismatch"):
        validate_dataset(dataset)


def test_missing_freeze_entry_is_not_silently_ignored(dataset):
    mutate(dataset, "freeze.json", lambda d: d.pop("tasks.holdout.json"))
    with pytest.raises(ValueError, match="freeze inventory"):
        validate_dataset(dataset)


def test_windows_checkout_line_endings_preserve_logical_freeze(dataset):
    for path in dataset.iterdir():
        if path.is_file():
            data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            path.write_bytes(data)
    assert validate_dataset(dataset)["structural_validation"] == "passed"
