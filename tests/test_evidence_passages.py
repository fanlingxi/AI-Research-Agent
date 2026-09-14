from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.benchmarking.passage_dataset import import_passages
from app.context.models import ContextBuildRequest, canonical_package_sha256
from app.context.service import ContextBuilderService
from app.knowledge.evidence_passages import passage_ranges
from app.knowledge.schemas import EvidenceSpan
from tests.test_context_neighbors import _setup


def test_live_trial_is_explicit_and_blocked_in_offline_tests(monkeypatch):
    from app.benchmarking.passage_trial import run

    assert run()["max_model_calls"] == 12
    monkeypatch.setenv("RUN_LIVE_LLM_INTEGRATION", "0")
    with pytest.raises(ValueError, match="offline"):
        run(execute=True)


def test_passages_preserve_offsets_and_mark_overlong_sentences():
    text = ("完整句子包含中文和引号。 \n" * 240) + "x" * 6500 + ". Ending."
    ranges = list(passage_ranges(text))
    assert "".join(text[a:b] for a, b, _ in ranges) == text
    assert all(0 < b - a <= 6000 for a, b, _ in ranges)
    assert any(hard for _, _, hard in ranges)
    assert ranges[0][1] >= 2400 and not ranges[0][2]
    assert list(passage_ranges("")) == []
    assert list(passage_ranges("One short sentence.")) == [(0, 19, False)]
    assert list(passage_ranges("x" * 5999 + ".5 rest"))[0] == (0, 6000, True)


def test_quote_contract_retains_old_values_and_rejects_unbounded_input():
    old = dict(paper_id="paper", chunk_id="chunk", page_start=1, page_end=1, quote="old")
    assert EvidenceSpan.model_validate(old).model_dump() == old
    assert len(EvidenceSpan(**{**old, "quote": "x" * 6000}).quote) == 6000
    with pytest.raises(ValidationError):
        EvidenceSpan(**{**old, "quote": "x" * 6001})


def test_new_reviewed_passages_preserve_old_snapshot_and_require_scope(tmp_path):
    repo, task, _ = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    old = builder.build_context(ContextBuildRequest(task_id=task.id, max_tokens=16000))
    text = "Needle method has a complete supported definition. " * 70
    paper = SimpleNamespace(
        id="long-paper",
        pdf_url="https://example.invalid/paper.pdf",
        pdf_pages=1,
        version="v1",
        pdf_sha256="pdf-hash",
        license="test",
        title="Long paper",
        pdf_path="paper.pdf",
        text=text,
        pages=[SimpleNamespace(start=0, end=len(text), pdf_page=1)],
    )
    mapping = import_passages(repo, SimpleNamespace(sources=[paper]), tmp_path)
    # New data in a different collection must not leak into the old task.
    still_scoped = builder.build_context(ContextBuildRequest(task_id=task.id, max_tokens=16000))
    assert not any(
        d.document_id == mapping["sources"][paper.id]["document_id"]
        for b in still_scoped.knowledge.claim_bundles
        for d in b.documents
    )
    memory = repo.memory_repository
    project = memory.create_project(
        name="Passage test", goal="Study needle", domain="research", metadata={}
    )
    memory.replace_project_knowledge_scopes(
        project.id,
        [mapping["sources"][paper.id]["scope"]],
        expected_project_revision=project.revision,
    )
    work = memory.create_workspace_task(
        project_id=project.id, title="Needle", goal="Needle method", priority="high", metadata={}
    )
    package = builder.build_context(
        ContextBuildRequest(
            task_id=work.id,
            max_tokens=16000,
            reading_format="inline-v1",
        )
    )
    quotes = [e.quote for b in package.knowledge.claim_bundles for e in b.evidence]
    assert any(len(q) > 600 for q in quotes)
    assert all(q in text for q in quotes)
    assert package.token_usage.used <= package.token_usage.budget
    assert (
        canonical_package_sha256(builder.snapshot_repository.get(old.snapshot_id))
        == old.package_sha256
    )
    for chunk_id, span in mapping["chunks"].items():
        chunk = next(
            c for b in package.knowledge.claim_bundles for c in b.chunks if c.chunk_id == chunk_id
        )
        assert chunk.content == text[span["start"] : span["end"]]


def test_long_quote_still_requires_valid_source_text(tmp_path):
    from app.knowledge.core_repository import ClaimEvidenceValidationError

    repo, _, _ = _setup(tmp_path, parts=["Needle " * 150, "Other " * 140])
    with repo.database.connect() as connection:
        with pytest.raises(ClaimEvidenceValidationError, match="located"):
            repo.core_repository._validate_evidence_tx(
                connection,
                EvidenceSpan(
                    paper_id="paper",
                    chunk_id="piece-0",
                    page_start=1,
                    page_end=1,
                    quote="fabricated " * 100,
                ),
            )
