"""Read-only A01 dataset validation. No settings, database, network, or model imports."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Split = Literal["dev", "holdout"]
Tag = Literal[
    "exact_name", "synonym", "multi_source", "cross_span", "conflict",
    "no_answer", "insufficient", "out_of_scope",
]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Review(Record):
    status: Literal["pending_human", "disputed", "human_approved"]
    author: Literal["codex_draft"]
    reviewer: Text | None
    reviewed_at: Text | None
    record_path: Text | None

    @model_validator(mode="after")
    def human_record_required(self):
        if self.status == "human_approved" and not all(
            (self.reviewer, self.reviewed_at, self.record_path)
        ):
            raise ValueError("human approval requires reviewer, date, and review record")
        if self.status == "pending_human" and any(
            (self.reviewer, self.reviewed_at, self.record_path)
        ):
            raise ValueError("pending drafts cannot claim completed human review")
        return self


class Source(Record):
    id: Identifier
    family_id: Identifier
    version: Literal["v1"]
    title: Text
    origin: Literal["repository_authored_synthetic"]
    usage: Literal["project_evaluation_only"]
    provenance: Text
    review_state: Literal["fixture_candidate"]
    text: Text
    text_sha256: Digest


class Span(Record):
    id: Identifier
    source_id: Identifier
    source_version: Literal["v1"]
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    quote: Text


class Corpus(Record):
    version: Literal["a01-corpus-v1"]
    sources: Annotated[list[Source], Field(min_length=1)]
    spans: Annotated[list[Span], Field(min_length=1)]


class Conclusion(Record):
    id: Identifier
    statement: Text
    evidence_span_ids: list[Identifier]
    basis: Literal["source", "scope_absence", "missing_information", "scope_boundary"]


class Expectation(Record):
    outcome: Literal["answer", "qualified_answer", "abstain", "needs_review", "scope_refusal"]
    required_conclusions: Annotated[list[Conclusion], Field(min_length=1)]
    forbidden_conclusions: Annotated[list[Text], Field(min_length=1)]
    required_evidence_span_ids: list[Identifier]
    conflict_span_ids: list[Identifier]
    missing_information: list[Text]
    rationale: Text


class Task(Record):
    id: Identifier
    version: Literal["v1"]
    family_id: Identifier
    split: Split
    tags: Annotated[list[Tag], Field(min_length=1)]
    target: Literal["project_run"]
    question: Text
    report_requirements: Annotated[list[Text], Field(min_length=1)]
    allowed_source_ids: Annotated[list[Identifier], Field(min_length=1)]
    forbidden_source_ids: list[Identifier]
    expectation: Expectation
    annotation: Review


class TaskFile(Record):
    protocol_version: Literal["a01-task-v1"]
    dataset_version: Literal["a01-synthetic-v1"]
    split: Split
    tasks: Annotated[list[Task], Field(min_length=1)]


class Family(Record):
    id: Identifier
    split: Split
    description: Text
    task_count: Literal[5]


class Manifest(Record):
    protocol_version: Literal["a01-task-v1"]
    dataset_version: Literal["a01-synthetic-v1"]
    corpus_version: Literal["a01-corpus-v1"]
    status: Literal["frozen_candidate_pending_human"]
    created_at: Literal["2026-09-13"]
    families: Annotated[list[Family], Field(min_length=8, max_length=8)]
    split_counts: dict[Split, int]
    required_tags: list[Tag]
    holdout_policy: Text
    model_execution: Literal["not_implemented_not_authorized"]


def _unique(values: list[str], location: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{location}: duplicate identities")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_dataset(
    root: Path, *, require_human_reviewed: bool = False, verify_freeze: bool = True
) -> dict:
    """Validate structure and cross-file invariants; never approve semantic labels."""
    manifest = Manifest.model_validate_json((root / "manifest.json").read_bytes())
    corpus = Corpus.model_validate_json((root / "corpus.json").read_bytes())
    task_files = [
        TaskFile.model_validate_json((root / f"tasks.{split}.json").read_bytes())
        for split in ("dev", "holdout")
    ]
    _unique([f.id for f in manifest.families], "families")
    _unique([s.id for s in corpus.sources], "sources")
    _unique([s.id for s in corpus.spans], "spans")
    tasks = [task for task_file in task_files for task in task_file.tasks]
    _unique([t.id for t in tasks], "tasks")
    _unique([" ".join(t.question.split()).casefold() for t in tasks], "questions")
    families = {f.id: f for f in manifest.families}
    sources = {s.id: s for s in corpus.sources}
    spans = {s.id: s for s in corpus.spans}
    _require(manifest.split_counts == {"dev": 25, "holdout": 15}, "v1 requires 25/15")
    _require(dict(Counter(t.split for t in tasks)) == manifest.split_counts, "split counts")
    _require(set(manifest.required_tags) == set(Tag.__args__), "required tag inventory")
    for family in families.values():
        _require(sum(t.family_id == family.id for t in tasks) == 5, "family task count")
    for source in sources.values():
        _require(source.family_id in families, f"{source.id}: unknown family")
        _require("\r" not in source.text, f"{source.id}: source text must use LF")
        _require(
            hashlib.sha256(source.text.encode("utf-8")).hexdigest() == source.text_sha256,
            f"{source.id}: text hash mismatch",
        )
    for span in spans.values():
        _require(span.source_id in sources, f"{span.id}: unknown source")
        source = sources[span.source_id]
        _require(span.source_version == source.version, f"{span.id}: stale version")
        _require(0 <= span.start < span.end <= len(source.text), f"{span.id}: invalid offset")
        _require(source.text[span.start:span.end] == span.quote, f"{span.id}: quote mismatch")
    for expected_split, task_file in zip(("dev", "holdout"), task_files, strict=True):
        _require(task_file.split == expected_split, "task file split mismatch")
        covered = {tag for t in task_file.tasks for tag in t.tags}
        _require(set(manifest.required_tags) <= covered, f"{expected_split}: missing coverage")
        for task in task_file.tasks:
            _require(task.family_id in families, f"{task.id}: unknown family")
            _require(
                task.split == task_file.split == families[task.family_id].split,
                f"{task.id}: family split leakage",
            )
            allowed, denied = set(task.allowed_source_ids), set(task.forbidden_source_ids)
            _unique(task.allowed_source_ids, f"{task.id}: allowed sources")
            _unique(task.forbidden_source_ids, f"{task.id}: forbidden sources")
            _unique(task.tags, f"{task.id}: tags")
            _require(not allowed & denied, f"{task.id}: overlapping scope")
            _require(allowed | denied <= sources.keys(), f"{task.id}: unknown source")
            _require(
                all(sources[s].family_id == task.family_id for s in allowed | denied),
                f"{task.id}: cross-family source leakage",
            )
            expectation = task.expectation
            conclusions = expectation.required_conclusions
            _unique([c.id for c in conclusions], f"{task.id}: conclusions")
            evidence = expectation.required_evidence_span_ids
            conflicts = expectation.conflict_span_ids
            _unique(evidence, f"{task.id}: evidence")
            _unique(conflicts, f"{task.id}: conflicts")
            _require(set(evidence) <= spans.keys(), f"{task.id}: unknown span")
            _require(
                all(spans[s].source_id in allowed for s in evidence),
                f"{task.id}: evidence outside allowed scope",
            )
            for conclusion in conclusions:
                _unique(conclusion.evidence_span_ids, f"{task.id}: conclusion spans")
                _require(
                    conclusion.basis != "source" or bool(conclusion.evidence_span_ids),
                    f"{task.id}: source conclusion lacks evidence",
                )
            _require(
                {s for c in conclusions for s in c.evidence_span_ids} == set(evidence),
                f"{task.id}: conclusion/evidence mismatch",
            )
            _require(set(conflicts) <= set(evidence), f"{task.id}: conflict evidence missing")
            if "conflict" in task.tags:
                _require(len(conflicts) >= 2, f"{task.id}: conflict needs both sides")
                _require(
                    expectation.outcome in {"qualified_answer", "needs_review"},
                    f"{task.id}: conflict cannot be an unconditional answer",
                )
            if "multi_source" in task.tags:
                _require(len({spans[s].source_id for s in evidence}) >= 2, "multi-source")
            if "cross_span" in task.tags:
                _require(len(evidence) >= 2, "cross-span")
            if "no_answer" in task.tags:
                _require(expectation.outcome == "abstain", "no-answer outcome")
                _require(not evidence, "no-answer cannot invent positive evidence")
            if "insufficient" in task.tags:
                _require(bool(expectation.missing_information), "missing information required")
                _require(expectation.outcome == "needs_review", "insufficient outcome")
            if "out_of_scope" in task.tags:
                _require(bool(denied), "out-of-scope needs a denied source")
                _require(expectation.outcome == "scope_refusal", "scope refusal required")
            if require_human_reviewed:
                _require(task.annotation.status == "human_approved", "pending human review")
                review_path = (root / str(task.annotation.record_path)).resolve()
                _require(review_path.is_relative_to(root.resolve()), "review path escapes dataset")
                _require(review_path.is_file(), "missing human review record")
    if verify_freeze:
        freeze = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
        expected_files = {
            "manifest.json", "corpus.json", "tasks.dev.json", "tasks.holdout.json",
            "schema.json", "PROTOCOL.md", "ANNOTATION_GUIDE.md", "SPLIT.md",
        }
        _require(set(freeze) == expected_files, "freeze inventory mismatch")
        for name, expected_digest in freeze.items():
            content = (root / name).read_text(encoding="utf-8").encode("utf-8")
            actual = hashlib.sha256(content).hexdigest()
            _require(actual == expected_digest, f"{name}: frozen file hash mismatch")
        _require(
            json.loads((root / "schema.json").read_text(encoding="utf-8")) == schemas(),
            "schema does not match validator",
        )
    return {
        "protocol_version": manifest.protocol_version,
        "dataset_version": manifest.dataset_version,
        "structural_validation": "passed",
        "tasks": len(tasks),
        "splits": dict(Counter(t.split for t in tasks)),
        "families": len(families),
        "sources": len(sources),
        "spans": len(spans),
        "annotation_status": dict(Counter(t.annotation.status for t in tasks)),
        "coverage": {
            split: dict(Counter(tag for t in tasks if t.split == split for tag in t.tags))
            for split in ("dev", "holdout")
        },
        "semantic_validation": "not_performed_by_this_validator",
        "model_calls": 0,
    }


def schemas() -> dict:
    return {cls.__name__: cls.model_json_schema() for cls in (Manifest, Corpus, TaskFile)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/research/v1"))
    parser.add_argument("--require-human-reviewed", action="store_true")
    args = parser.parse_args()
    result = validate_dataset(args.dataset, require_human_reviewed=args.require_human_reviewed)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
