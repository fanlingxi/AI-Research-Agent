import pytest

from app.benchmarking.retrieval_compare import (
    LocalHashProjection,
    retrieval_metrics,
    span_recall,
    summarize,
)
from tests.test_query_consistency import CHUNK
from tests.test_reports import _repository_with_paper


def _span(start, end, source="paper"):
    return {"source_id": source, "start": start, "end": end}


def test_metrics_distinguish_hit_from_full_span_recall_and_ranking():
    gold = [_span(10, 30)]
    units = [[_span(10, 20)], [_span(20, 30)]]
    assert span_recall(gold, units[:1]) == 0
    assert span_recall(gold, units) == 1
    assert span_recall(gold, [[_span(10, 30, "outside")]]) == 0
    ranked = [[_span(40, 50)], *units]
    metrics = retrieval_metrics(gold, ranked, ranked)
    assert metrics["hit@5"] == 1 and metrics["recall@5"] == 1
    assert metrics["mrr@5"] == 0.5
    assert 0 < metrics["ndcg@5"] < 1
    assert all(value is None for value in retrieval_metrics([], ranked, ranked).values())


def test_failed_trials_remain_in_macro_denominator_and_no_gold_is_separate():
    good = retrieval_metrics([_span(0, 10)], [[_span(0, 10)]], [[_span(0, 10)]])
    bad = retrieval_metrics([_span(0, 10)], [], [[_span(0, 10)]])
    empty = retrieval_metrics([], [], [])
    rows = [dict(path=path, strategy=strategy, status=status, metrics=metrics,
                 latency_ms=latency, gold_count=gold, scope_violation=False)
            for path in ("project_run", "quick_report")
            for strategy in ("legacy", "bm25-v1", "hybrid-v1")
            for status, metrics, latency, gold in [
                ("ok", good, 1, 1), ("failed", bad, 2, 1), ("ok", empty, 3, 0),
            ]]
    summary = summarize(rows)["project_run"]["hybrid-v1"]
    assert summary["attempted"] == 3 and summary["failed"] == 1 and summary["no_gold"] == 1
    assert summary["macro"]["recall@10"] == pytest.approx(0.5)
    assert summary["p95_latency_ms"] == 3


def test_local_projection_is_scoped_version_pinned_and_deterministic(tmp_path):
    repository, _ = _repository_with_paper(tmp_path)
    projection = LocalHashProjection(repository)
    assert projection.search("evidence", allowed_paper_ids=set(), top_k=10) == []
    hits = projection.search("evidence", allowed_paper_ids={"paper:formal"}, top_k=10)
    assert [hit.chunk_id for hit in hits] == [CHUNK]
    assert hits[0].source_identity.chunk_id == CHUNK
    assert projection.search("evidence", allowed_paper_ids={"paper:formal"}, top_k=10) == hits
