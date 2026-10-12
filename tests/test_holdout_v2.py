"""AI-consensus holdout-v2: structure, label provenance and offline runner boundaries."""

from __future__ import annotations

import json
import shutil

import pytest

from app.benchmarking import holdout_v2
from scripts.validate_holdout_v2 import DATA, DATASET, validate_dataset


@pytest.fixture
def dataset(tmp_path):
    target = tmp_path / "holdout-v2"
    shutil.copytree(DATASET, target)
    return target


def mutate(root, filename, change):
    path = root / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_frozen_dataset_is_marked_ai_consensus_not_human_gold():
    result = validate_dataset(DATASET, data=None)
    assert result["label_provenance"] == "ai-consensus"
    assert result["human_verified"] is False
    assert set(result["splits"]) == {"dev", "holdout"}
    assert result["splits"]["holdout"] > result["splits"]["dev"]
    assert set(result["annotation_status"]) <= {"consensus", "adjudicated"}


@pytest.mark.parametrize(
    ("filename", "change", "error"),
    [
        ("manifest.json", lambda d: d.update(human_verified=True), "human_verified"),
        ("corpus.json", lambda d: d["spans"][0].update(quote="invented"), "quote length"),
        ("corpus.json", lambda d: d["spans"][0].update(start=10**7, end=10**7 + 5), "outside"),
        (
            "tasks.holdout.json",
            lambda d: d["tasks"][0]["annotation"].update(label_provenance="human"),
            "label_provenance",
        ),
        (
            "tasks.holdout.json",
            lambda d: d["tasks"][0].update(allowed_source_ids=["factscore"]),
            "cross-family scope",
        ),
    ],
)
def test_validation_rejects_relabelled_or_leaking_data(dataset, filename, change, error):
    mutate(dataset, filename, change)
    with pytest.raises(ValueError, match=error):
        validate_dataset(dataset, data=None, verify_freeze=False)


def test_freeze_detects_any_tracked_change(dataset):
    (dataset / "README.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="freeze hash"):
        validate_dataset(dataset, data=None)


def test_model_facing_cases_never_carry_expectations_or_labels():
    cases = holdout_v2.cases("holdout")
    assert cases and all(set(c) == {"id", "question", "source_ids"} for c in cases)
    raw = json.dumps(cases, ensure_ascii=False)
    for leaked in ("expectation", "forbidden", "conclusion", "annotation", "ai-consensus"):
        assert leaked not in raw
    plan = holdout_v2.plan("holdout")
    assert plan["runs"] == 2 * len(cases) and plan["workflows"] == ["research_v4", "research_v7"]


def test_real_runner_stays_disabled_inside_offline_tests():
    with pytest.raises(ValueError, match="offline"):
        holdout_v2.prepare()
    with pytest.raises(ValueError, match="offline"):
        holdout_v2.execute("not-a-real-path")


@pytest.mark.skipif(not (DATA / "papers").is_dir(), reason="local PDFs are not distributed")
def test_local_pdfs_reproduce_every_frozen_quote():
    result = validate_dataset(DATASET, data=DATA)
    assert result["quotes_checked_against_pdf_text"] == len(
        json.loads((DATASET / "corpus.json").read_text(encoding="utf-8"))["spans"]
    )
