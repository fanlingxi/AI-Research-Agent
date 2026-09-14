"""Validate the real-paper benchmark without network, models, or application stores."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from app.tools.file_hash import file_digest
from scripts.validate_research_dataset import (
    Digest,
    Identifier,
    Record,
    Split,
    Task,
    Text,
    _require,
    _unique,
)


class Page(Record):
    pdf_page: Annotated[int, Field(ge=1)]
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]


class Paper(Record):
    id: Identifier
    family_id: Identifier
    split: Split
    version: Text
    title: Text
    authors: Annotated[list[Text], Field(min_length=1)]
    landing_url: Text
    pdf_url: Text
    doi: Text | None
    fetched_at: Text
    pdf_path: Text
    pdf_sha256: Digest
    pdf_pages: Annotated[int, Field(ge=1)]
    license: Literal["CC-BY-4.0"]
    license_url: Literal["https://creativecommons.org/licenses/by/4.0/"]
    license_evidence_url: Text
    license_evidence: Text
    attribution: Text
    transformations: Text
    extractor: Text
    text: Text
    text_sha256: Digest
    pages: Annotated[list[Page], Field(min_length=1)]


class PaperSpan(Record):
    id: Identifier
    source_id: Identifier
    source_version: Text
    pdf_page: Annotated[int, Field(ge=1)]
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    quote: Text


class PaperCorpus(Record):
    version: Literal["a01-papers-corpus-v1"]
    sources: Annotated[list[Paper], Field(min_length=8, max_length=8)]
    spans: Annotated[list[PaperSpan], Field(min_length=1)]


class PaperTasks(Record):
    protocol_version: Literal["a01-paper-task-v1"]
    dataset_version: Literal["a01-papers-v1"]
    split: Split
    tasks: Annotated[list[Task], Field(min_length=1)]


class PaperFamily(Record):
    id: Identifier
    split: Split
    task_count: Annotated[int, Field(ge=1)]
    source_ids: Annotated[list[Identifier], Field(min_length=2)]


class PaperManifest(Record):
    protocol_version: Literal["a01-paper-task-v1"]
    dataset_version: Literal["a01-papers-v1"]
    corpus_version: Literal["a01-papers-corpus-v1"]
    status: Literal["candidate_pending_human"]
    language: Literal["zh_questions_en_sources"]
    created_at: Text
    families: Annotated[list[PaperFamily], Field(min_length=4, max_length=4)]
    split_counts: dict[Split, int]
    budget_id: Literal["s0-deepseek-flash-20260913"]
    coverage_note: Text
    raw_papers_are_evaluation_input: Literal[True]
    annotations_are_model_input: Literal[False]


def _contained_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    _require(path.is_relative_to(root.resolve()), "file path escapes dataset")
    _require(path.is_file(), f"missing file: {relative}")
    return path


def schemas() -> dict:
    return {
        cls.__name__: cls.model_json_schema()
        for cls in (PaperManifest, PaperCorpus, PaperTasks)
    }


def validate_dataset(root: Path, *, verify_freeze: bool = True) -> dict:
    manifest = PaperManifest.model_validate_json((root / "manifest.json").read_bytes())
    corpus = PaperCorpus.model_validate_json((root / "corpus.json").read_bytes())
    task_files = [
        PaperTasks.model_validate_json((root / f"tasks.{split}.json").read_bytes())
        for split in ("dev", "holdout")
    ]
    tasks = [t for tf in task_files for t in tf.tasks]
    for values, name in [
        ([s.id for s in corpus.sources], "papers"),
        ([s.pdf_sha256 for s in corpus.sources], "paper contents"),
        ([s.id for s in corpus.spans], "spans"),
        ([t.id for t in tasks], "tasks"),
        ([t.question.strip().casefold() for t in tasks], "questions"),
        ([f.id for f in manifest.families], "families"),
    ]:
        _unique(values, name)
    _require(manifest.split_counts == {"dev": 25, "holdout": 15}, "split contract")
    _require(dict(Counter(t.split for t in tasks)) == manifest.split_counts, "split counts")
    sources = {s.id: s for s in corpus.sources}
    spans = {s.id: s for s in corpus.spans}
    families = {f.id: f for f in manifest.families}
    listed = [s for f in manifest.families for s in f.source_ids]
    _unique(listed, "cross-family paper leakage")
    _require(set(listed) == set(sources), "family paper inventory")
    for family in manifest.families:
        _require(
            sum(t.family_id == family.id for t in tasks) == family.task_count,
            "family task count",
        )
        for sid in family.source_ids:
            s = sources[sid]
            _require(s.family_id == family.id and s.split == family.split, "paper split leakage")
    for s in corpus.sources:
        pdf = _contained_file(root, s.pdf_path)
        _require(pdf.read_bytes().startswith(b"%PDF-"), "not a PDF")
        _require(hashlib.sha256(pdf.read_bytes()).hexdigest() == s.pdf_sha256, "PDF hash")
        _require(hashlib.sha256(s.text.encode()).hexdigest() == s.text_sha256, "text hash")
        _require(len(s.pages) == s.pdf_pages, "page count")
        cursor = 0
        for number, page in enumerate(s.pages, 1):
            _require(page.pdf_page == number, "page order")
            _require(page.start == cursor and page.end > page.start, "page coverage")
            _require(page.end <= len(s.text), "page outside text")
            cursor = page.end
            if number < s.pdf_pages:
                _require(s.text[cursor:cursor + 2] == "\n\n", "page separator")
                cursor += 2
        _require(cursor == len(s.text), "incomplete page coverage")
    for span in corpus.spans:
        _require(span.source_id in sources, "unknown source")
        s = sources[span.source_id]
        _require(span.source_version == s.version, "source version mismatch")
        _require(span.pdf_page <= s.pdf_pages, "invalid PDF page")
        page = s.pages[span.pdf_page - 1]
        _require(page.start <= span.start < span.end <= page.end, "span outside page")
        _require(s.text[span.start:span.end] == span.quote, "quote mismatch")
    for expected_split, tf in zip(("dev", "holdout"), task_files, strict=True):
        _require(tf.split == expected_split, "task file split")
        for t in tf.tasks:
            _require(t.family_id in families, "unknown task family")
            family = families[t.family_id]
            _require(t.split == tf.split == family.split, "task split leakage")
            allowed, denied = set(t.allowed_source_ids), set(t.forbidden_source_ids)
            _unique(t.allowed_source_ids, "allowed scope")
            _unique(t.forbidden_source_ids, "forbidden scope")
            _require(not allowed & denied, "scope overlap")
            _require(allowed | denied <= set(family.source_ids), "cross-family scope")
            e = t.expectation
            required = set(e.required_evidence_span_ids)
            _unique(e.required_evidence_span_ids, "required spans")
            _unique([c.id for c in e.required_conclusions], "conclusions")
            _require(required <= set(spans), "unknown evidence")
            _require(all(spans[s].source_id in allowed for s in required), "denied evidence")
            _require(
                {s for c in e.required_conclusions for s in c.evidence_span_ids} == required,
                "conclusion evidence mismatch",
            )
            for c in e.required_conclusions:
                _unique(c.evidence_span_ids, "conclusion spans")
                _require(c.basis != "source" or bool(c.evidence_span_ids), "missing support")
            _require(not e.conflict_span_ids and "conflict" not in t.tags, "invented conflict")
            if "multi_source" in t.tags:
                _require(len({spans[s].source_id for s in required}) >= 2, "multi-paper evidence")
            if "cross_span" in t.tags:
                _require(len(required) >= 2, "cross-span evidence")
            if "no_answer" in t.tags:
                _require(e.outcome == "abstain" and not required, "no-answer contract")
            if "insufficient" in t.tags:
                _require(
                    e.outcome == "needs_review" and bool(e.missing_information), "missing info"
                )
            if "out_of_scope" in t.tags:
                _require(e.outcome == "scope_refusal" and bool(denied), "scope refusal")
            _require(t.annotation.status == "pending_human", "v1 must not auto-approve labels")
    if verify_freeze:
        freeze = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
        actual_files = {
            p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and p.name != "freeze.json"
        }
        required_files = {
            "manifest.json", "corpus.json", "tasks.dev.json", "tasks.holdout.json",
            "schema.json", "README.md", "TASKS.md", "provenance/acl-license.html",
            "provenance/cc-by-4.html",
        } | {s.pdf_path for s in corpus.sources} | {
            f"provenance/{s.id}.html" for s in corpus.sources
        }
        _require(actual_files == required_files == set(freeze), "freeze inventory")
        for name, digest in freeze.items():
            _require(file_digest(_contained_file(root, name)) == digest, f"freeze hash: {name}")
        _require(
            json.loads((root / "schema.json").read_text(encoding="utf-8")) == schemas(),
            "schema mismatch",
        )
    return {
        "status": "structural_validation_passed",
        "dataset_version": manifest.dataset_version,
        "papers": len(sources), "pdf_pages": sum(s.pdf_pages for s in corpus.sources),
        "tasks": len(tasks), "splits": dict(Counter(t.split for t in tasks)),
        "paper_families": len(families), "spans": len(spans),
        "coverage": dict(Counter(tag for t in tasks for tag in t.tags)),
        "annotation_status": dict(Counter(t.annotation.status for t in tasks)),
        "model_calls": 0,
        "limitations": [
            "human semantics pending", "no live quality evaluation", "no strict conflict cases"
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/research/papers-v1"))
    args = parser.parse_args()
    print(json.dumps(validate_dataset(args.dataset), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
