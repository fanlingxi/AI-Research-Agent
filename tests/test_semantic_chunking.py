from types import SimpleNamespace

import pytest

from app.benchmarking.embedding_trial import (
    VARIANTS,
    choose_chunking,
    choose_model,
    quality_gate,
    run,
    summarize,
)
from app.benchmarking.passage_dataset import import_passages
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.semantic_chunking import pdf_heading_offsets, semantic_ranges, structure_ranges


class TopicEncoder:
    def embed_documents(self, texts):
        return [[1.0, 0.0] if "alpha" in text else [0.0, 1.0] for text in texts]


def test_semantic_boundaries_follow_topic_and_keep_exact_offsets():
    unit = "alpha " + "a" * 392 + ". "
    text = unit * 4 + ("beta " + "b" * 393 + ". ") * 6
    ranges = semantic_ranges(text, TopicEncoder())
    assert ranges[0][1] == 4 * len(unit)
    assert "".join(text[a:b] for a, b, _ in ranges) == text
    assert all(0 < b - a <= 6000 for a, b, _ in ranges)
    long = "x" * 12001
    assert any(hard for _, _, hard in semantic_ranges(long, TopicEncoder()))
    assert semantic_ranges("", TopicEncoder()) == []
    bad = SimpleNamespace(embed_documents=lambda texts: [1.0] * len(texts))
    with pytest.raises(ValueError, match="segmentation"):
        semantic_ranges(text, bad)


def test_pdf_headings_use_frozen_offsets_and_reject_changed_extraction(tmp_path, monkeypatch):
    raw = "Introduction\nA short opening.\n2 Methods\nDetails follow."
    frozen = " ".join(raw.split())
    page = SimpleNamespace(pdf_page=1, start=0, end=len(frozen))
    paper = SimpleNamespace(pdf_path="source.pdf", text=frozen, pages=[page])
    monkeypatch.setattr(
        "pypdf.PdfReader",
        lambda path: SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: raw)]),
    )
    offsets = pdf_heading_offsets(paper, tmp_path)
    assert offsets == [frozen.index("2 Methods")]
    ranges = structure_ranges(frozen, offsets)
    assert frozen[ranges[1][0] : ranges[1][1]].startswith("2 Methods")
    assert "".join(frozen[a:b] for a, b, _ in ranges) == frozen
    paper.text = "changed " + frozen
    with pytest.raises(ValueError, match="frozen"):
        pdf_heading_offsets(paper, tmp_path)


def test_cross_page_passages_are_reviewed_with_both_pages_and_original_text(tmp_path):
    text = "The definition starts here and continues on page two."
    paper = SimpleNamespace(
        id="cross-page",
        pdf_url="https://example.invalid/paper.pdf",
        pdf_pages=2,
        version="v1",
        pdf_sha256="test",
        license="test",
        title="Cross page",
        pdf_path="paper.pdf",
        text=text,
        pages=[
            SimpleNamespace(start=0, end=26, pdf_page=1),
            SimpleNamespace(start=26, end=len(text), pdf_page=2),
        ],
    )
    repo = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    mapping = import_passages(
        repo,
        SimpleNamespace(sources=[paper]),
        tmp_path,
        version="structure-v1",
        ranges_by_source={paper.id: [(0, len(text), False)]},
    )
    span = next(iter(mapping["chunks"].values()))
    assert (span["pdf_page"], span["pdf_page_end"]) == (1, 2)
    published = repo.list_published_entities()[0]
    assert published.evidence[0].quote == text
    assert (published.evidence[0].page_start, published.evidence[0].page_end) == (1, 2)


def test_incomplete_comparison_cannot_select_winner_or_pass_quality_gate(monkeypatch):
    assert run()["planned"] == 350
    monkeypatch.setenv("RUN_LIVE_LLM_INTEGRATION", "0")
    with pytest.raises(ValueError, match="offline"):
        run(execute=True)
    rows = [
        dict(
            chunking="sentence-2400-v2",
            model=m,
            strategy=s,
            path=p,
            status="ok",
            gold_count=1,
            metrics={"recall@10": 0.5, "mrr@10": 0.5, "ndcg@10": 0.5},
        )
        for m, s in VARIANTS
        for p in ("project_run", "quick_report")
    ]
    groups = summarize(rows)
    assert choose_model(groups) is not None
    baseline = [g for g in groups if g["model"] == "current" and g["strategy"] == "legacy"]
    assert len(baseline) == 2
    assert not any(g["passed"] for g in quality_gate(groups, baseline))
    rows[-1].update(status="failed", metrics={})
    assert choose_model(summarize(rows)) is None
    rows[-1]["status"] = "not_attempted"
    assert choose_model(summarize(rows)) is None


def test_chunking_retains_sentence_policy_when_guard_fails():
    candidate = {"model": "qwen3-local", "strategy": "hybrid-v1"}
    rows = [
        dict(
            chunking=cut,
            **candidate,
            path=path,
            status="ok",
            gold_count=1,
            metrics={"recall@10": 0.5, "mrr@10": 0.5, "ndcg@10": 0.5},
        )
        for cut in ("sentence-2400-v2", "structure-v1", "semantic-v1")
        for path in ("project_run", "quick_report")
    ]
    baseline = summarize(rows[:2])
    assert (
        choose_chunking(summarize(rows[2:]), baseline, candidate)["chunking"] == "sentence-2400-v2"
    )
    for row in rows[2:4]:
        row["metrics"]["recall@10"] = 0.8
        row["metrics"]["mrr@10"] = 0.4
    assert (
        choose_chunking(summarize(rows[2:]), baseline, candidate)["chunking"] == "sentence-2400-v2"
    )
    for row in rows[2:4]:
        row["metrics"]["mrr@10"] = 0.5
    assert choose_chunking(summarize(rows[2:]), baseline, candidate)["chunking"] == "structure-v1"
    rows[-1].update(status="failed", metrics={})
    assert choose_chunking(summarize(rows[2:]), baseline, candidate) is None


def test_new_question_validation_never_runs_models_in_offline_tests(monkeypatch):
    from app.benchmarking.embedding_validation import run as validate

    assert validate()["max_model_calls"] == 12
    monkeypatch.setenv("RUN_LIVE_LLM_INTEGRATION", "0")
    with pytest.raises(ValueError, match="offline"):
        validate(execute=True)
