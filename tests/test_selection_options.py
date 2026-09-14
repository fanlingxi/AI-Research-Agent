import copy
import json
from types import SimpleNamespace

import pytest

from app.benchmarking.selection_options import (
    encoded,
    parse_order,
    reading_view,
    rerank,
    restore_view,
    run,
)


def test_reading_view_restores_quotes_semantics_and_versioned_relationships():
    passage = "The method does NOT guarantee correctness under distribution shift. " * 5
    payload = {
        "task": {"goal": passage},
        "knowledge": {
            "claim_bundles": [
                {
                    "claim": {"claim_id": "c1", "statement": passage, "status": "published"},
                    "evidence": [{"evidence_id": "e1", "chunk_id": "k1", "quote": passage}],
                    "chunks": [{"chunk_id": "k1", "content": passage}],
                    "sources": [{"source_id": "s1", "version": 2, "content_sha256": "abc"}],
                    "selection": {"reason": "this is an audit record"},
                    "created_at": "yesterday",
                },
                {
                    "claim": {"claim_id": "c2", "statement": passage},
                    "evidence": [{"evidence_id": "e2", "chunk_id": "k1", "quote": passage}],
                },
            ]
        },
    }
    original = copy.deepcopy(payload)
    for slim in (False, True):
        view, sidecar = reading_view(payload, slim=slim)
        assert restore_view(view, sidecar) == original == payload
        assert len(view["texts"]) == 1
        assert len(encoded(view)) < len(encoded(payload))
        body = restore_view(view)
        assert body["knowledge"]["claim_bundles"][0]["sources"][0]["version"] == 2
        assert body["knowledge"]["claim_bundles"][0]["claim"]["status"] == "published"
        assert body["knowledge"]["claim_bundles"][1]["evidence"][0]["evidence_id"] == "e2"
        assert ("selection" in body["knowledge"]["claim_bundles"][0]) is not slim


def test_reading_view_does_not_merge_nearly_equal_evidence_or_lose_unrelated_metadata():
    first = "A" * 100
    payload = {
        "knowledge": {"chunks": [first, first + " NOT"]},
        "task": {"selection": {"required": True}},
    }
    view, sidecar = reading_view(payload, slim=True)
    assert len(view["texts"]) == 2 and not sidecar
    assert restore_view(view) == payload
    with pytest.raises(ValueError, match="Reserved"):
        reading_view({"knowledge": {"$text": 0}})


@pytest.mark.parametrize(
    "response",
    [
        '{"indices":[0,0]}',
        '{"indices":[-1]}',
        '{"indices":[3]}',
        '{"indices":[true]}',
        '{"indices":["1"]}',
        '{"indices":[]}',
        '{"indices":[1],"extra":"value"}',
        "not json",
    ],
)
def test_reranking_rejects_invalid_indices(response):
    with pytest.raises(ValueError):
        parse_order(response, 3)


def test_partial_ranking_preserves_all_remaining_candidates_in_order():
    assert parse_order('{"indices":[3,1]}', 5) == [3, 1, 0, 2, 4]


def test_model_timeout_keeps_original_order_and_unknown_usage(tmp_path):
    class Failing:
        last_usage = {"input_tokens": 999}
        last_usage_complete = True
        last_response_model = None

        def invoke(self, *args, **kwargs):
            raise TimeoutError("private error details must not be logged")

    ranks = [
        SimpleNamespace(
            bundle=SimpleNamespace(
                claim={"id": "claim"},
                evidence=[SimpleNamespace(chunk={"id": "chunk", "content": "text"})],
            )
        )
    ]
    result, record = rerank(Failing(), "question", ranks, tmp_path)
    assert result is ranks and record["status"] == "fallback" and record["usage"] is None
    assert record["model_calls"] == 1 and record["error_type"] == "TimeoutError"
    assert "private error" not in (tmp_path / "call.json").read_text()
    assert json.loads((tmp_path / "request.json").read_text())["prompt"]


def test_live_trial_remains_disabled_under_pytest(offline_trial_inputs):
    offline_trial_inputs("selection_options")
    with pytest.raises(ValueError, match="disabled"):
        run(execute=True)
