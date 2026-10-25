"""Lossless compact reading, frozen-format compatibility and actual runtime budgets."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.models import AgentRunCreateRequest
from app.agent.tools import research_input_payload
from app.context.models import (
    ContextBuildRequest,
    ContextPackage,
    canonical_package_sha256,
    runtime_context_payload,
    runtime_context_token_count,
)
from app.context.reading import research_payload
from app.context.service import ContextBuilderService
from app.knowledge.core_repository import _normalize_for_match
from app.knowledge.schemas import CandidateEntity, EvidenceSpan
from tests.test_answer_coverage import anchored_report
from tests.test_context_neighbors import _setup
from tests.test_research_workflow import _analysis, _plan, _research_stack

FIXTURES = Path(__file__).parent / "fixtures"


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fixed_package(fmt):
    raw = json.loads((FIXTURES / "context_snapshot_legacy_v1.json").read_text("utf-8"))["package"]
    if fmt != "legacy":
        raw.update(
            reading_format=fmt,
            package_schema_version="1.2",
            retrieval_audit={"strategy_id": "fixed-reading-fixture", "parameters": {}, "scope": {}},
        )
    package = ContextPackage.model_validate(raw)
    package.package_sha256 = canonical_package_sha256(package)
    return package


def _expand_compact_payload(payload):
    """Test-only inverse: exact inline values, including absent locator keys and list order."""
    result = deepcopy(payload)
    result.pop("reading_notes")
    knowledge = result["knowledge"]
    tables = {name: knowledge.pop(name) for name in ("documents", "sources", "chunks")}
    for bundle in knowledge["claim_bundles"]:
        for name, id_field in (
            ("documents", "document_ids"), ("sources", "source_ids"), ("chunks", "chunk_ids")
        ):
            bundle[name] = [deepcopy(tables[name][key]) for key in bundle.pop(id_field)]
        quotes = {e["evidence_id"]: e["quote"] for e in bundle["evidence"]}
        for field in ("statement", "object_value"):
            value = bundle["claim"][field]
            if isinstance(value, dict):
                bundle["claim"][field] = quotes[value["same_as_evidence_quote"]]
        for evidence in bundle["evidence"]:
            if evidence.pop("quote_normalized_derived", False):
                evidence["location"]["locator"]["quote_normalized"] = _normalize_for_match(
                    evidence["quote"].strip()
                )
    return result


@pytest.mark.parametrize("fmt", ["legacy", "research-v1", "inline-v1"])
def test_old_formats_keep_frozen_reading_bytes_tokens_and_package_hash(fmt):
    # These baselines were computed before compact-v1 was implemented.
    expected = json.loads((FIXTURES / "reading_format_baselines.json").read_text("utf-8"))[fmt]
    package = _fixed_package(fmt)
    assert canonical_package_sha256(package) == expected["package_sha256"]
    for name, payload in (
        ("runtime", runtime_context_payload(package)), ("research", research_payload(package))
    ):
        assert hashlib.sha256(_encoded(payload).encode()).hexdigest() == (
            expected[f"{name}_payload_sha256"]
        )
    assert runtime_context_token_count(package) == expected["tokens"]


@pytest.mark.parametrize("locator", [
    {"quote_normalized": "a full quote. strasse", "page": 2},
    {"quote_normalized": "Different original locator", "page": 2},
    {"page": 2},
    {"quote_normalized": None},
    None,
    "free-form locator",
])
def test_compact_roundtrip_preserves_all_inline_data_and_snapshot(locator):
    package = _fixed_package("compact-v1")
    first = package.knowledge.claim_bundles[0]
    quote = "  A full\n quote. Straße  "
    first.evidence[0].quote = quote
    first.evidence[0].location = {
        "locator": deepcopy(locator), "created_at": "source attribute",
        "metadata": {"selection": "literal provenance", "legacy_id": "keep"},
        "quote_normalized_derived": "also free-form",
    }
    first.claim.statement = first.claim.object_value = quote
    first.chunks[0].location = {"metadata": {"updated_at": "keep"}}
    second = first.model_copy(deep=True)
    second.claim.claim_id = "second"
    second.evidence[0].evidence_id = "second-evidence"
    second.evidence[0].claim_id = "second"
    second.claim.statement = quote.strip()  # Nearly equal must remain literal.
    second.claim.object_value = "An independent value"
    second.evidence[0].location = {}  # No locator must remain absent.
    package.knowledge.claim_bundles = [first, second]
    original = package.model_dump(mode="json")
    compact = research_payload(package)
    inline = research_payload(package.model_copy(update={"reading_format": "inline-v1"}))
    assert _expand_compact_payload(compact) == inline
    assert package.model_dump(mode="json") == original
    assert research_payload(package) == compact
    for name in ("documents", "sources", "chunks"):
        assert len(compact["knowledge"][name]) == len(getattr(first, name))
        assert name not in compact["knowledge"]["claim_bundles"][0]
    bundles = compact["knowledge"]["claim_bundles"]
    assert bundles[0]["claim"]["statement"] == {
        "same_as_evidence_quote": first.evidence[0].evidence_id
    }
    assert bundles[1]["claim"]["statement"] == quote.strip()
    assert all(b["evidence"][0]["quote"] == quote for b in bundles)
    derived = isinstance(locator, dict) and locator.get("quote_normalized") == (
        _normalize_for_match(quote.strip())
    )
    assert bundles[0]["evidence"][0].get("quote_normalized_derived", False) == derived


@pytest.mark.parametrize("name,field", [
    ("documents", "title"), ("sources", "version"), ("chunks", "content"),
])
def test_conflicting_id_is_rejected_without_mutating_snapshot(name, field):
    package = _fixed_package("compact-v1")
    second = package.knowledge.claim_bundles[0].model_copy(deep=True)
    setattr(getattr(second, name)[0], field, "Conflicting value")
    package.knowledge.claim_bundles.append(second)
    before = package.model_dump(mode="json")
    with pytest.raises(ValueError, match="compact-v1: conflicting"):
        research_payload(package)
    assert package.model_dump(mode="json") == before


def test_ambiguous_evidence_ids_do_not_create_lossy_claim_references():
    package = _fixed_package("compact-v1")
    bundle = package.knowledge.claim_bundles[0]
    bundle.claim.statement = bundle.evidence[0].quote
    other = bundle.evidence[0].model_copy(update={"quote": "Different quote"})
    bundle.evidence.append(other)
    compact = research_payload(package)
    assert isinstance(compact["knowledge"]["claim_bundles"][0]["claim"]["statement"], str)
    assert _expand_compact_payload(compact) == research_payload(
        package.model_copy(update={"reading_format": "inline-v1"})
    )


def _shared_source_setup(tmp_path):
    text = ("Needle method preserves complete evidence and source provenance. " * 40).strip()
    repo, task, _ = _setup(tmp_path, parts=[text], publish_last=True)
    scope = repo.memory_repository.list_project_knowledge_scopes(task.project_id)[0].collection_slug
    with repo.database.connect() as db:
        ingestion_id = db.execute("SELECT id FROM ingestions LIMIT 1").fetchone()[0]
    for index in range(1, 6):
        candidate = CandidateEntity(
            id=f"shared-{index}", ingestion_id=ingestion_id, topic_slug=scope,
            name=f"Needle excerpt {index}", type="Paper", summary=text, confidence=1,
            evidence=EvidenceSpan(paper_id="paper", chunk_id="piece-0", quote=text,
                                  page_start=1, page_end=1),
        )
        repo.add_candidate_entity(candidate)
        repo.publish_entity(candidate.id, review_note="test shared evidence")
    return repo, task


@pytest.mark.parametrize("budget", [6000, 16000, 32000])
def test_real_builder_packs_more_whole_bundles_within_actual_budget(tmp_path, budget):
    repo, task = _shared_source_setup(tmp_path)
    builder = ContextBuilderService(repo)
    packages = {
        fmt: builder.build_context(ContextBuildRequest(
            task_id=task.id, reading_format=fmt, max_tokens=budget
        )) for fmt in ("inline-v1", "compact-v1")
    }
    inline, compact = packages.values()
    assert len(compact.knowledge.claim_bundles) >= len(inline.knowledge.claim_bundles)
    if budget < 32000:
        assert len(compact.knowledge.claim_bundles) > len(inline.knowledge.claim_bundles)
    for package in packages.values():
        assert package.token_usage.used == runtime_context_token_count(package) <= budget
        assert research_input_payload(package) == runtime_context_payload(package)
        restored = builder.snapshot_repository.get(package.snapshot_id)
        assert restored == package
        assert restored.package_schema_version == "1.2"
        assert canonical_package_sha256(restored) == package.package_sha256
    assert compact.retrieval_audit.parameters["reading_format"] == "compact-v1"
    # Compare the same selected bundles, not differently truncated sets.
    inline_chars = len(_encoded(research_payload(
        compact.model_copy(update={"reading_format": "inline-v1"})
    )))
    compact_chars = len(_encoded(research_payload(compact)))
    assert compact_chars < inline_chars
    print(f"budget={budget}: bundles inline={len(inline.knowledge.claim_bundles)}, "
          f"compact={len(compact.knowledge.claim_bundles)}; same-bundle chars "
          f"{inline_chars} -> {compact_chars}; compact tokens={compact.token_usage.used}")


def test_requests_accept_compact_but_frozen_snapshots_cannot_be_reformatted():
    assert ContextBuildRequest(task_id="task", reading_format="compact-v1").reading_format == (
        "compact-v1"
    )
    assert AgentRunCreateRequest(reading_format="compact-v1").reading_format == "compact-v1"
    with pytest.raises(ValidationError, match="cannot be reranked or reformatted"):
        AgentRunCreateRequest(context_snapshot_id="frozen", reading_format="compact-v1")
    for fmt in ("legacy", "research-v1", "inline-v1"):
        old = _fixed_package(fmt)
        altered = old.model_copy(update={"reading_format": "compact-v1"})
        assert canonical_package_sha256(altered) != old.package_sha256


@pytest.mark.parametrize("verbatim", [True, False])
@pytest.mark.parametrize("existing_snapshot", [True, False])
def test_research_v8_uses_compact_snapshot_and_validates_original_quotes(
    tmp_path, verbatim, existing_snapshot
):
    _, service, worker, project, task, _, bundle, llm = _research_stack(
        tmp_path, [_analysis(), _plan()]
    )
    finding = anchored_report(bundle, task.goal)["findings"][0]
    if not verbatim:
        finding["support"][0]["quote"] = "Fabricated support text."
    value = {"title": "Compact report", "answer_parts": [
        {"question_part": task.goal, "findings": [finding], "gap": ""}
    ]}
    llm.responses.extend([json.dumps(value)] * 2)
    if existing_snapshot:
        frozen = service.context_builder.build_context(ContextBuildRequest(
            task_id=task.id, reading_format="compact-v1", max_tokens=16000
        ))
        options = {"context_snapshot_id": frozen.snapshot_id}
    else:
        options = {"reading_format": "compact-v1"}
    run = service.create_run(project.id, task.id, AgentRunCreateRequest(
        workflow="research_v8", token_budget=32000, **options
    ))
    frozen = service.load_context_snapshot(run.id)
    assert frozen.reading_format == "compact-v1"
    assert worker.run_once()
    done = service.get_run(run.id)
    assert done.status == ("completed" if verbatim else "needs_review"), done.error_message
    package = service.load_context_snapshot(run.id)
    assert package == frozen
    payload = json.loads(llm.calls[-1].split("Research input: ", 1)[1].split("\n", 1)[0])
    assert payload == research_payload(package)
    for original, compact in zip(
        package.knowledge.claim_bundles, payload["knowledge"]["claim_bundles"], strict=True
    ):
        assert [e["quote"] for e in compact["evidence"]] == [e.quote for e in original.evidence]
    if verbatim:
        assert done.repair_count == 0
        assert "[cite:" in service.repository.get_output(run.id).rendered_text
    else:
        assert "exact contiguous quote" in llm.calls[-1]
