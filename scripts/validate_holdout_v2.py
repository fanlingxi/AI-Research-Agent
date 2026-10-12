"""Validate the AI-consensus holdout-v2 benchmark without network, models, or stores.

Full paper texts are not tracked. When the local evaluation folder holds the PDFs, every
page is re-extracted and each quote is checked verbatim at its recorded offsets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.tools.file_hash import file_digest
from scripts.validate_paper_benchmark import Page, PaperSpan
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

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks/research/holdout-v2"
DATA = ROOT / "data/evaluation/holdout-v2"
DATA_V3 = Path("D:/AI-Research-Agent/data/evaluation/holdout-v3")
DatasetVersion = Literal["research-holdout-v2", "research-holdout-v3"]


def dataset_version(dataset: str = "v2") -> DatasetVersion:
    version = dataset if dataset.startswith("research-holdout-") else f"research-holdout-{dataset}"
    if version not in ("research-holdout-v2", "research-holdout-v3"):
        raise ValueError(f"Unsupported dataset: {dataset}")
    return version


def dataset_path(dataset: str = "v2") -> Path:
    return DATASET.parent / dataset_version(dataset).removeprefix("research-")


def data_path(dataset: str = "v2") -> Path:
    return DATA if dataset_version(dataset) == "research-holdout-v2" else DATA_V3


Answerability = Literal["answerable", "partially_answerable", "not_answerable", "out_of_scope"]
OUTCOME_ANSWERABILITY = {
    "answer": "answerable", "qualified_answer": "answerable",
    "needs_review": "partially_answerable", "abstain": "not_answerable",
    "scope_refusal": "out_of_scope",
}


class HoldoutPaper(Record):
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
    landing_page_sha256: Digest
    attribution: Text
    transformations: Text
    extractor: Text
    text_sha256: Digest
    text_chars: Annotated[int, Field(ge=1)]
    pages: Annotated[list[Page], Field(min_length=1)]


class HoldoutCorpus(Record):
    version: Literal["research-holdout-v2-corpus"]
    full_text_tracked: Literal[False]
    sources: Annotated[list[HoldoutPaper], Field(min_length=6, max_length=10)]
    spans: Annotated[list[PaperSpan], Field(min_length=1)]


class LabelerVerdict(Record):
    labeler: Text
    answerability: Answerability | None
    answerability_agrees: bool | None
    points: Literal["full", "partial", "none", "missing"]
    quote_overlap: Literal["all", "some", "none", "not_applicable", "missing"]


class ConsensusAnnotation(Record):
    label_provenance: Literal["ai-consensus"]
    human_verified: Literal[False]
    drafted_by: Text
    verdicts: Annotated[list[LabelerVerdict], Field(min_length=2)]
    tie_breaker: LabelerVerdict | None
    status: Literal["consensus", "adjudicated"]
    adjudication: Text
    record_path: Text

    @model_validator(mode="after")
    def consensus_means_agreement(self):
        independent = [v for v in self.verdicts if v.answerability is not None]
        if self.status == "consensus" and not any(v.answerability_agrees for v in independent):
            raise ValueError("consensus requires at least one agreeing independent labeler")
        return self


class HoldoutTask(Task):
    kind: Literal[
        "single_fact", "synthesis", "comparison", "limitation",
        "insufficient_evidence", "out_of_scope", "overclaim_trap",
    ]
    difficulty: Literal["easy", "medium", "hard"]
    annotation: ConsensusAnnotation


class HoldoutTasks(Record):
    protocol_version: Literal["research-holdout-v2-task"]
    dataset_version: Literal["research-holdout-v2"]
    split: Split
    tasks: Annotated[list[HoldoutTask], Field(min_length=1)]


class HoldoutFamily(Record):
    id: Identifier
    split: Split
    task_count: Annotated[int, Field(ge=1)]
    source_ids: Annotated[list[Identifier], Field(min_length=2)]


class HoldoutManifest(Record):
    protocol_version: Literal["research-holdout-v2-task"]
    dataset_version: Literal["research-holdout-v2"]
    corpus_version: Literal["research-holdout-v2-corpus"]
    status: Literal["frozen_ai_consensus"]
    label_provenance: Literal["ai-consensus"]
    human_verified: Literal[False]
    language: Literal["zh_questions_en_sources"]
    created_at: Text
    labelers: Annotated[list[Text], Field(min_length=3)]
    families: Annotated[list[HoldoutFamily], Field(min_length=2)]
    split_counts: dict[Split, int]
    dropped_tasks: list[dict[str, str]]
    decision_use: Text
    coverage_note: Text
    raw_papers_are_evaluation_input: Literal[True]
    annotations_are_model_input: Literal[False]


class V3ConsensusAnnotation(ConsensusAnnotation):
    @model_validator(mode="after")
    def consensus_means_agreement(self):
        # The bootstrap dataset explicitly records missing reviews, never invented agreement.
        if self.adjudication.startswith("待仲裁") and all(
            v.answerability is None and v.answerability_agrees is None
            and v.points == "missing" and v.quote_overlap == "missing"
            for v in self.verdicts
        ):
            return self
        return super().consensus_means_agreement()


class V3HoldoutTask(HoldoutTask):
    annotation: V3ConsensusAnnotation


class V3HoldoutTasks(HoldoutTasks):
    protocol_version: Literal["research-holdout-v3-task"]
    dataset_version: Literal["research-holdout-v3"]
    tasks: Annotated[list[V3HoldoutTask], Field(min_length=1)]


class V3HoldoutCorpus(HoldoutCorpus):
    version: Literal["research-holdout-v3-corpus"]


class V3HoldoutManifest(HoldoutManifest):
    protocol_version: Literal["research-holdout-v3-task"]
    dataset_version: Literal["research-holdout-v3"]
    corpus_version: Literal["research-holdout-v3-corpus"]


def models_for(version: str):
    if dataset_version(version) == "research-holdout-v3":
        return V3HoldoutManifest, V3HoldoutCorpus, V3HoldoutTasks
    return HoldoutManifest, HoldoutCorpus, HoldoutTasks


def infer_version(root: Path, version: str | None = None) -> DatasetVersion:
    actual = dataset_version(json.loads(
        (root / "manifest.json").read_text(encoding="utf-8")
    )["dataset_version"])
    _require(version is None or dataset_version(version) == actual, "dataset version mismatch")
    return actual


def schemas(version: str = "v2") -> dict:
    models = models_for(version)
    return {c.__name__: c.model_json_schema() for c in models}


def extract_pages(pdf: Path) -> tuple[str, list[dict]]:
    """Corpus normalization: whitespace collapsed per page, pages joined by two LF."""
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font notices do not affect plain text
    text, pages = "", []
    for number, page in enumerate(PdfReader(pdf).pages, 1):
        content = re.sub(r"\s+", " ", page.extract_text(extraction_mode="plain") or "").strip()
        if number > 1:
            text += "\n\n"
        pages.append({"pdf_page": number, "start": len(text), "end": len(text) + len(content)})
        text += content
    return text, pages


def load_tasks(root: Path = DATASET, *, version: str | None = None) -> list[HoldoutTask]:
    _, _, task_model = models_for(infer_version(root, version))
    files = [
        task_model.model_validate_json((root / f"tasks.{split}.json").read_bytes())
        for split in ("dev", "holdout")
    ]
    for split, tf in zip(("dev", "holdout"), files, strict=True):
        _require(tf.split == split, "task file split")
    return [t for tf in files for t in tf.tasks]


def validate_dataset(
    root: Path = DATASET, *, data: Path | None | Literal["auto"] = "auto", verify_freeze=True,
    version: str | None = None,
):
    version = infer_version(root, version)
    if data == "auto":
        data = data_path(version)
    manifest_model, corpus_model, _ = models_for(version)
    manifest = manifest_model.model_validate_json((root / "manifest.json").read_bytes())
    corpus = corpus_model.model_validate_json((root / "corpus.json").read_bytes())
    tasks = load_tasks(root, version=version)
    for values, name in [
        ([s.id for s in corpus.sources], "papers"),
        ([s.pdf_sha256 for s in corpus.sources], "paper contents"),
        ([s.id for s in corpus.spans], "spans"),
        ([t.id for t in tasks], "tasks"),
        ([" ".join(t.question.split()).casefold() for t in tasks], "questions"),
        ([f.id for f in manifest.families], "families"),
    ]:
        _unique(values, name)
    _require(dict(Counter(t.split for t in tasks)) == manifest.split_counts, "split counts")
    sources = {s.id: s for s in corpus.sources}
    spans = {s.id: s for s in corpus.spans}
    families = {f.id: f for f in manifest.families}
    listed = [s for f in manifest.families for s in f.source_ids]
    _unique(listed, "cross-family paper leakage")
    _require(set(listed) == set(sources), "family paper inventory")
    _require({f.split for f in manifest.families} == {"dev", "holdout"}, "both splits")
    for family in manifest.families:
        _require(sum(t.family_id == family.id for t in tasks) == family.task_count, "family count")
        for sid in family.source_ids:
            s = sources[sid]
            _require(s.family_id == family.id and s.split == family.split, "paper split leakage")
    texts = {}
    for s in corpus.sources:
        _require(len(s.pages) == s.pdf_pages, "page count")
        cursor = 0
        for number, page in enumerate(s.pages, 1):
            _require(page.pdf_page == number and page.start == cursor, "page coverage")
            _require(page.end >= page.start, "page order")
            cursor = page.end + (2 if number < s.pdf_pages else 0)
        _require(cursor == s.text_chars, "incomplete page coverage")
        pdf = (data / s.pdf_path) if data else None
        if pdf and pdf.is_file():
            _require(hashlib.sha256(pdf.read_bytes()).hexdigest() == s.pdf_sha256, "PDF hash")
            text, pages = extract_pages(pdf)
            _require(hashlib.sha256(text.encode()).hexdigest() == s.text_sha256, "text hash")
            _require(pages == [p.model_dump() for p in s.pages], "page offsets")
            texts[s.id] = text
    for span in corpus.spans:
        _require(span.source_id in sources, "unknown source")
        s = sources[span.source_id]
        _require(span.source_version == s.version, "source version mismatch")
        _require(span.pdf_page <= s.pdf_pages, "invalid PDF page")
        page = s.pages[span.pdf_page - 1]
        _require(page.start <= span.start < span.end <= page.end, "span outside page")
        _require(span.end - span.start == len(span.quote), "quote length")
        if span.source_id in texts:
            _require(texts[span.source_id][span.start:span.end] == span.quote, "quote mismatch")
    for t in tasks:
        family = families.get(t.family_id)
        _require(family is not None and t.split == family.split, "task split leakage")
        allowed, denied = set(t.allowed_source_ids), set(t.forbidden_source_ids)
        _require(not allowed & denied, "scope overlap")
        if version == "research-holdout-v3":
            # A v3 refusal can ask about a paper belonging to another family;
            # only the allowed papers can enter the model's context.
            _require(allowed <= set(family.source_ids), "cross-family scope")
            _require(denied <= set(sources), "unknown forbidden source")
        else:
            _require(allowed | denied <= set(family.source_ids), "cross-family scope")
        e = t.expectation
        required = set(e.required_evidence_span_ids)
        _require(required <= set(spans), "unknown evidence")
        _require(all(spans[s].source_id in allowed for s in required), "denied evidence")
        _require(
            {s for c in e.required_conclusions for s in c.evidence_span_ids} == required,
            "conclusion evidence mismatch",
        )
        for c in e.required_conclusions:
            _require(c.basis != "source" or bool(c.evidence_span_ids), "missing support")
        _require(not e.conflict_span_ids and "conflict" not in t.tags, "invented conflict")
        if "multi_source" in t.tags:
            _require(len({spans[s].source_id for s in required}) >= 2, "multi-paper evidence")
        if "cross_span" in t.tags:
            _require(len(required) >= 2, "cross-span evidence")
        if "no_answer" in t.tags:
            if version == "research-holdout-v3":
                # Evidence can support WHY the requested result is unavailable.
                _require(e.outcome == "abstain" and bool(e.missing_information)
                         and any(c.basis in {"missing_information", "scope_absence"}
                                 for c in e.required_conclusions), "no-answer contract")
            else:
                _require(e.outcome == "abstain" and not required, "no-answer contract")
        if "insufficient" in t.tags:
            _require(e.outcome == "needs_review" and bool(e.missing_information), "missing info")
        if "out_of_scope" in t.tags:
            _require(e.outcome == "scope_refusal" and bool(denied), "scope refusal")
        _require((t.kind == "out_of_scope") == ("out_of_scope" in t.tags), "kind/tag mismatch")
    if verify_freeze:
        freeze = json.loads((root / "freeze.json").read_text(encoding="utf-8"))
        actual = {
            p.relative_to(root).as_posix() for p in root.rglob("*")
            if p.is_file() and p.name != "freeze.json"
        }
        _require(actual == set(freeze), "freeze inventory")
        for name, digest in freeze.items():
            _require(file_digest(root / name) == digest, f"freeze hash: {name}")
        _require(
            json.loads((root / "schema.json").read_text(encoding="utf-8")) == schemas(version),
            "schema mismatch",
        )
    return {
        "status": "structural_validation_passed",
        "dataset_version": manifest.dataset_version,
        "label_provenance": manifest.label_provenance,
        "human_verified": manifest.human_verified,
        "papers": len(sources),
        "pdf_pages": sum(s.pdf_pages for s in corpus.sources),
        "quotes_checked_against_pdf_text": sum(s.source_id in texts for s in corpus.spans),
        "tasks": len(tasks),
        "splits": dict(Counter(t.split for t in tasks)),
        "kinds": dict(Counter(t.kind for t in tasks)),
        "annotation_status": dict(Counter(t.annotation.status for t in tasks)),
        "model_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--version", choices=["v2", "v3"])
    parser.add_argument("--data", type=Path)
    args = parser.parse_args()
    version = infer_version(args.dataset, args.version)
    print(json.dumps(validate_dataset(args.dataset, data=args.data or data_path(version),
                                      version=version), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
