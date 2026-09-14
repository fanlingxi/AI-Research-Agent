"""Real-paper provenance and scope validation, without remote services."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader

from scripts.validate_paper_benchmark import validate_dataset

ROOT = Path(__file__).resolve().parents[1] / "benchmarks/research/papers-v1"


@pytest.fixture
def dataset(tmp_path):
    target = tmp_path / "papers"
    shutil.copytree(ROOT, target)
    return target


def mutate(root, filename, change):
    path = root / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_real_papers_are_versioned_candidates_with_expected_split():
    result = validate_dataset(ROOT)
    assert result["papers"] == 8
    assert result["pdf_pages"] == 131
    assert result["splits"] == {"dev": 25, "holdout": 15}
    assert result["annotation_status"] == {"pending_human": 40}


def test_all_pdf_pages_reproduce_the_stored_text():
    corpus = json.loads((ROOT / "corpus.json").read_text(encoding="utf-8"))
    for source in corpus["sources"]:
        pdf = PdfReader(ROOT / source["pdf_path"])
        assert len(pdf.pages) == source["pdf_pages"]
        for page, location in zip(pdf.pages, source["pages"], strict=True):
            text = re.sub(r"\s+", " ", page.extract_text(extraction_mode="plain") or "").strip()
            assert text == source["text"][location["start"]:location["end"]]


@pytest.mark.parametrize(
    ("filename", "change", "error"),
    [
        ("corpus.json", lambda d: d["spans"][0].update(pdf_page=99), "invalid PDF page"),
        ("corpus.json", lambda d: d["spans"][0].update(pdf_page=1), "span outside page"),
        ("corpus.json", lambda d: d["spans"][0].update(quote="invented"), "quote mismatch"),
        ("corpus.json", lambda d: d["spans"][0].update(source_version="arxiv-v0"), "version"),
        ("corpus.json", lambda d: d["sources"][0].update(text="changed"), "text hash"),
        ("corpus.json", lambda d: d["sources"][0].update(license="unknown"), "license"),
        (
            "corpus.json",
            lambda d: d["sources"][0].update(pdf_path="../../../../README.md"),
            "path escapes",
        ),
        (
            "corpus.json",
            lambda d: d["sources"][0]["pages"][0].update(end=5),
            "page separator",
        ),
        (
            "manifest.json",
            lambda d: d["families"][0].update(split="holdout"),
            "paper split leakage",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0].update(allowed_source_ids=["ares"]),
            "denied evidence",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0]["allowed_source_ids"].append("lost-middle"),
            "cross-family scope",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0]["expectation"].update(conflict_span_ids=["r-aspects"]),
            "invented conflict",
        ),
        (
            "tasks.dev.json",
            lambda d: d["tasks"][0]["annotation"].update(status="human_approved"),
            "human approval requires",
        ),
        (
            "manifest.json",
            lambda d: d.update(annotations_are_model_input=True),
            "annotations_are_model_input",
        ),
    ],
)
def test_invalid_provenance_and_scope_are_rejected(dataset, filename, change, error):
    mutate(dataset, filename, change)
    with pytest.raises(ValueError, match=error):
        validate_dataset(dataset, verify_freeze=False)


def test_pdf_replacement_breaks_provenance(dataset):
    path = dataset / "papers/ragas.pdf"
    path.write_bytes(path.read_bytes() + b"\nmodified")
    with pytest.raises(ValueError, match="PDF hash"):
        validate_dataset(dataset, verify_freeze=False)


def test_unregistered_files_are_not_silently_frozen(dataset):
    (dataset / "unregistered-answer.txt").write_text("unreviewed data", encoding="utf-8")
    with pytest.raises(ValueError, match="freeze inventory"):
        validate_dataset(dataset)


def test_question_change_breaks_frozen_candidate(dataset):
    mutate(dataset, "tasks.dev.json", lambda d: d["tasks"][0].update(question="另一道题"))
    with pytest.raises(ValueError, match="freeze hash"):
        validate_dataset(dataset)
