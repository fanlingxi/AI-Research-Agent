"""Offline capacity comparisons retain production boundaries and interval semantics."""

from types import SimpleNamespace

import pytest

from app.benchmarking.context_expansion import preview_capacities, run, span_trace
from tests.test_run_feedback import stack as stack


def test_previews_do_not_persist_or_modify_archived_snapshot(stack):
    repo, service, _, _, task, package, *_ = stack
    with repo.database.connect() as db:
        before = db.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0]
    previews = preview_capacities(service.context_builder, package)
    assert [p.token_usage.budget for p in previews] == [16000, 65536, 262144] * 2
    assert all(p.token_usage.used <= p.token_usage.budget for p in previews)
    assert all(p.constraints.collection_scopes == package.constraints.collection_scopes
               for p in previews)
    with repo.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == before
    assert service.context_builder.snapshot_repository.get(package.snapshot_id) == package
    repo.memory_repository.update_workspace_task(task.id, expected_revision=task.revision,
                                                 goal="Different research question")
    with pytest.raises(ValueError, match="Task or project changed"):
        preview_capacities(service.context_builder, package)


def test_diagnosis_rejects_tampered_snapshot_and_paid_test_execution(
    stack, tmp_path, offline_trial_inputs,
):
    offline_trial_inputs("context_expansion")
    service, package = stack[1], stack[5]
    bad = package.model_copy(update={"package_sha256": "0" * 64})
    with pytest.raises(ValueError, match="integrity"):
        preview_capacities(service.context_builder, bad)
    with pytest.raises(ValueError, match="offline tests"):
        run(execute=True)
    with pytest.raises(ValueError, match="managed context expansion"):
        run(followup=tmp_path)


def test_interval_coverage_requires_every_piece_and_matching_source_version():
    mapping = {}
    selections = []
    for key, start, end, selected in [("a", 0, 6, True), ("b", 6, 12, False)]:
        mapping[key] = {"source_id": "paper", "source_version": "v1", "claim_id": key,
                        "text_sha256": key, "start": start, "end": end}
        selections.append(SimpleNamespace(
            item_id=key, rank=1 if selected else 20, score=0.2,
            selected=selected, reason="selected" if selected else "context_budget",
            sources=[SimpleNamespace(chunk_id=key, source_version="v1", chunk_sha256=key)]))
    package = SimpleNamespace(
        retrieval_audit=SimpleNamespace(selections=selections, strategy_id="legacy"),
        token_usage=SimpleNamespace(budget=16000, used=1000),
        diagnostics=SimpleNamespace(verified_claim_bundles=2),
        knowledge=SimpleNamespace(claim_bundles=[1]))
    target = {"id": "span", "source_id": "paper", "source_version": "v1", "start": 2, "end": 10}
    assert span_trace(package, mapping, [target])["covered_spans"] == 0
    selections[1].selected = True
    assert span_trace(package, mapping, [target])["covered_spans"] == 1
    mapping["b"]["start"] = 7  # A gap cannot be counted as full evidence coverage.
    assert span_trace(package, mapping, [target])["covered_spans"] == 0
    mapping["b"]["source_version"] = "stale"
    with pytest.raises(ValueError, match="version or identity"):
        span_trace(package, mapping, [target])
