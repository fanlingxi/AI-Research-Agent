"""Fixed evidence uses the current formal read and existing budget packer."""

import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.context.models import ContextBuildRequest, runtime_context_payload
from app.context.service import ContextBuilderService, _fingerprint
from app.research_profiles import CLASSIC, STANDARD
from app.retrieval.reranking import input_digest
from tests.test_context_builder import _formal_entity
from tests.test_context_neighbors import _setup as _neighbor_setup
from tests.test_retrieval_audit import _context_setup


@pytest.mark.parametrize("profile", [CLASSIC, STANDARD])
def test_profiles_preserve_pins(profile):
    request = ContextBuildRequest(task_id="task", pinned_claim_ids=["claim"])
    assert profile.context_request(request).pinned_claim_ids == ["claim"]


def test_pin_limit():
    with pytest.raises(ValidationError):
        ContextBuildRequest(task_id="task", pinned_claim_ids=[str(i) for i in range(6)])


@pytest.mark.parametrize("strategy", ["legacy", "bm25-v1", "hybrid-v1", "dense-v1"])
@pytest.mark.parametrize("reading_format", ["legacy", "research-v1", "inline-v1"])
def test_empty_pins_preserve_request_fingerprint_and_package_bytes(
    tmp_path, monkeypatch, strategy, reading_format,
):
    repo, _, task, _ = _context_setup(tmp_path)
    monkeypatch.setattr("app.context.service._now", lambda: "2026-01-01T00:00:00+00:00")
    builder = ContextBuilderService(repo, vector_retriever=Mock(retrieve=Mock(return_value=[])))
    request = ContextBuildRequest(task_id=task.id, snapshot_id="fixed", retrieval_strategy=strategy,
                                  reading_format=reading_format,
                                  enable_vector_candidates=strategy == "dense-v1")
    assert "pinned_claim_ids" not in request.model_dump(mode="json")
    old_request_payload = request.model_dump(mode="json")
    package = builder.preview_context(request)
    explicit_empty = builder.preview_context(request.model_copy(update={"pinned_claim_ids": []}))
    assert explicit_empty.model_dump_json() == package.model_dump_json()
    assert "pinned_claims" not in package.retrieval_audit.parameters
    assert package.request_fingerprint == _fingerprint({
        "request": old_request_payload, "project_id": package.project.project_id,
        "project_revision": package.project.revision, "task_revision": package.task.revision,
        "collection_scopes": package.constraints.collection_scopes,
    })
    assert runtime_context_payload(explicit_empty) == runtime_context_payload(package)


def test_pins_recover_formal_evidence_absent_from_dense_candidates(tmp_path):
    repo, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repo, vector_retriever=Mock(retrieve=Mock(return_value=[])))
    formal = builder.preview_context(ContextBuildRequest(task_id=task.id))
    claim_id = formal.knowledge.claim_bundles[0].claim.claim_id
    request = ContextBuildRequest(task_id=task.id, retrieval_strategy="dense-v1",
                                  enable_vector_candidates=True)
    assert builder.preview_context(request).knowledge.claim_bundles == []
    pinned = builder.build_context(request.model_copy(update={"pinned_claim_ids": [claim_id]}))
    assert pinned.knowledge.claim_bundles[0].claim.claim_id == claim_id
    assert pinned.retrieval_audit.parameters["pinned_claims"]["selected"] == [claim_id]
    assert builder.snapshot_repository.get(pinned.snapshot_id) == pinned


def test_pins_are_inserted_after_coverage_selection(tmp_path, monkeypatch):
    repo, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repo)
    formal = builder.preview_context(ContextBuildRequest(task_id=task.id))
    claim_id = formal.knowledge.claim_bundles[0].claim.claim_id
    builder.coverage_reranker = Mock(rank=lambda payload: {
        "status": "fallback", "reason": "fixture", "input_sha256": input_digest(payload),
    })
    selected = []

    def select_none(ranked, record):
        selected.extend(item.bundle.claim["id"] for item in ranked)
        return []

    monkeypatch.setattr("app.context.service.apply_reranking", select_none)
    pinned = builder.build_context(ContextBuildRequest(
        task_id=task.id, evidence_reranking="coverage-v2", pinned_claim_ids=[claim_id],
    ))
    assert claim_id in selected
    assert pinned.knowledge.claim_bundles[0].claim.claim_id == claim_id
    assert pinned.retrieval_audit.parameters["pinned_claims"]["selected"] == [claim_id]


def test_oversized_pin_is_dropped_and_audited(tmp_path):
    repo, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repo)
    full = builder.preview_context(ContextBuildRequest(task_id=task.id))
    claim_id = full.knowledge.claim_bundles[0].claim.claim_id
    small = builder.build_context(ContextBuildRequest(
        task_id=task.id, max_tokens=full.token_usage.used - 100, pinned_claim_ids=[claim_id],
    ))
    assert small.knowledge.claim_bundles == []
    assert small.token_usage.used <= small.token_usage.budget
    assert small.retrieval_audit.parameters["pinned_claims"] == {
        "requested": [claim_id], "verified": [claim_id], "selected": [],
        "ignored": [
            {"claim_id": claim_id, "reason": "pin_budget_share"},
            {"claim_id": claim_id, "reason": "context_budget"},
        ],
    }


@pytest.mark.parametrize("sql", [
    "UPDATE claims SET status = 'draft'",
    "DELETE FROM project_knowledge_scopes",
    "UPDATE sources SET superseded_by_source_id = id",
])
def test_direct_pins_cannot_bypass_formal_read(tmp_path, sql):
    repo, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repo)
    full = builder.preview_context(ContextBuildRequest(task_id=task.id))
    claim_id = full.knowledge.claim_bundles[0].claim.claim_id
    with repo._connect() as connection:
        connection.execute(sql)
    package = builder.build_context(ContextBuildRequest(
        task_id=task.id, pinned_claim_ids=[claim_id],
    ))
    assert package.knowledge.claim_bundles == []
    assert package.retrieval_audit.parameters["pinned_claims"] == {
        "requested": [claim_id], "verified": [], "selected": [],
        "ignored": [{"claim_id": claim_id, "reason": "not_in_formal_bundles"}],
    }


def test_pinned_neighbor_group_survives_before_unpinned_bundles(tmp_path):
    repo, task, claims = _neighbor_setup(tmp_path)
    _formal_entity(repo, collection_slug="allowed", entity_id="other",
                   title="Needle", suffix="Other evidence. " * 350)
    builder = ContextBuilderService(repo)
    request = ContextBuildRequest(
        task_id=task.id, context_neighbors="adjacent-v1", max_tokens=16000,
    )
    full = builder.preview_context(request)
    assert len(full.knowledge.claim_bundles) == 4
    package = builder.build_context(request.model_copy(update={
        "max_tokens": full.token_usage.used - 100, "pinned_claim_ids": [claims[2]],
    }))
    assert package.knowledge.claim_bundles[0].claim.claim_id == claims[2]
    assert {b.claim.claim_id for b in package.knowledge.claim_bundles} == set(claims)
    assert package.retrieval_audit.parameters["pinned_claims"]["selected"] == [claims[2]]


def test_over_share_neighbor_group_stays_in_original_order(tmp_path):
    repo, task, claims = _neighbor_setup(tmp_path)
    builder = ContextBuilderService(repo)
    request = ContextBuildRequest(task_id=task.id, context_neighbors="adjacent-v1")
    full = builder.preview_context(request)
    original = [b.claim.claim_id for b in full.knowledge.claim_bundles]
    package = builder.preview_context(request.model_copy(update={
        "max_tokens": full.token_usage.used + 20,
        "pinned_claim_ids": [claims[2], claims[0]],
    }))
    assert [b.claim.claim_id for b in package.knowledge.claim_bundles] == original
    audit = package.retrieval_audit.parameters["pinned_claims"]
    assert audit["selected"] == [claims[2], claims[0]]
    assert audit["ignored"] == [
        {"claim_id": key, "reason": "pin_budget_share"} for key in [claims[2], claims[0]]
    ]


@pytest.mark.parametrize("reading_format", ["legacy", "research-v1", "inline-v1"])
@pytest.mark.parametrize("pin_count", [1, 2])
def test_pin_share_preserves_task_leader(tmp_path, reading_format, pin_count):
    repo, task, _ = _neighbor_setup(
        tmp_path, parts=["Needle method definition. " * 100] * 3, publish_last=True,
    )
    builder = ContextBuilderService(repo)
    request = ContextBuildRequest(task_id=task.id, max_tokens=32000,
                                  reading_format=reading_format)
    full = builder.preview_context(request)
    claims = [b.claim.claim_id for b in full.knowledge.claim_bundles]
    assert len(claims) == 3
    # Leave room for two complete bundles plus the envelope, but not all three.
    bundles = runtime_context_payload(full)["knowledge"]["claim_bundles"]
    costs = [max(1, (len(json.dumps(b, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"))) + 3) // 4) for b in bundles]
    budget = full.token_usage.used - min(costs) + 50
    assert max(costs) <= budget // 2 < sum(costs[1:])
    pins = claims[1:1 + pin_count]
    package = builder.build_context(request.model_copy(update={
        "max_tokens": budget, "pinned_claim_ids": pins,
    }))
    assert [b.claim.claim_id for b in package.knowledge.claim_bundles] == [claims[1], claims[0]]
    assert package.token_usage.used <= budget
    audit = package.retrieval_audit.parameters["pinned_claims"]
    assert audit["verified"] == pins
    assert audit["selected"] == [claims[1]]
    assert audit["ignored"] == ([
        {"claim_id": claims[2], "reason": "pin_budget_share"},
        {"claim_id": claims[2], "reason": "context_budget"},
    ] if pin_count == 2 else [])


def test_pin_revalidates_after_external_retrieval(tmp_path):
    repo, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repo)
    full = builder.preview_context(ContextBuildRequest(task_id=task.id))
    claim_id = full.knowledge.claim_bundles[0].claim.claim_id

    def revoke(*args, **kwargs):
        with repo._connect() as connection:
            connection.execute("UPDATE sources SET superseded_by_source_id = id")
        return []

    builder.vector_retriever = Mock(retrieve=Mock(side_effect=revoke))
    package = builder.build_context(ContextBuildRequest(
        task_id=task.id, enable_vector_candidates=True, pinned_claim_ids=[claim_id],
    ))
    assert package.knowledge.claim_bundles == []
    assert package.retrieval_audit.parameters["preparation_attempts"] == 2
    assert package.retrieval_audit.parameters["pinned_claims"] == {
        "requested": [claim_id], "verified": [], "selected": [],
        "ignored": [{"claim_id": claim_id, "reason": "not_in_formal_bundles"}],
    }


def test_standard_profile_fallback_preserves_pins(tmp_path):
    repo, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repo)
    full = builder.preview_context(ContextBuildRequest(task_id=task.id))
    claim_id = full.knowledge.claim_bundles[0].claim.claim_id
    builder.vector_retriever = Mock(retrieve=Mock(side_effect=RuntimeError("offline")))
    package = builder.build_research_context(ContextBuildRequest(
        task_id=task.id, pinned_claim_ids=[claim_id],
    ), profile="standard")
    assert "research_profile:classic" in package.diagnostics.notices
    assert package.retrieval_audit.parameters["pinned_claims"]["selected"] == [claim_id]
