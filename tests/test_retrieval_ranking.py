from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from app.context.knowledge_reader import RawEvidence, RawKnowledgeBundle
from app.context.knowledge_reader import query_terms as context_query_terms
from app.context.ranking import rank_bundles
from app.context.retrieval import CandidateReference
from app.knowledge.query import _query_terms, _rank_evidence
from app.knowledge.schemas import ReportEvidence
from app.retrieval.ranking import query_terms, rank_scored_candidates, term_coverage

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "retrieval_legacy_v1.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=lambda case: case["name"])
def test_report_ranking_matches_pre_extraction_outputs(case):
    evidence = [ReportEvidence(**item) for item in case["report_evidence"]]
    before = [item.model_dump() for item in evidence]
    ranked = _rank_evidence(case["query"], evidence, top_k=case["top_k"])
    assert [item.model_dump() for item in ranked] == case["expected_report"]
    assert [item.model_dump() for item in evidence] == before
    assert _query_terms(case["query"]) == case["terms"]


@pytest.mark.parametrize("case", _FIXTURE["cases"], ids=lambda case: case["name"])
def test_bundle_ranking_matches_pre_extraction_outputs(case):
    bundles = [
        RawKnowledgeBundle(
            **{**item, "evidence": [RawEvidence(**entry) for entry in item["evidence"]]}
        )
        for item in deepcopy(case["bundles"])
    ]
    references = [CandidateReference(**item) for item in case["references"]]
    before = [asdict(bundle) for bundle in bundles]
    ranked = rank_bundles(bundles, query=case["query"], candidate_references=references)
    assert [
        {
            "claim_id": item.bundle.claim["id"],
            "rank": item.rank,
            "score": item.score,
            "score_breakdown": item.score_breakdown,
            "channels": item.channels,
            "selected_reason": item.selected_reason,
        }
        for item in ranked
    ] == case["expected_bundles"]
    assert [asdict(bundle) for bundle in bundles] == before
    assert context_query_terms(case["query"]) == case["terms"]


def test_shared_ordering_preserves_score_scale_ties_and_group_cap():
    # Scores need not be probabilities; the caller controls their scale.
    candidates = [("b", "p1", 10.0), ("a", "p1", 10.0), ("c", "p1", 8.0), ("d", "p2", -2.0)]
    ranked = rank_scored_candidates(
        iter(candidates),
        score=lambda item: item[2],
        identity=lambda item: (item[0],),
        group=lambda item: item[1],
        max_per_group=2,
    )
    assert ranked == [candidates[1], candidates[0], candidates[3]]
    assert ranked[0] is candidates[1]
    assert candidates[0][0] == "b"


def test_shared_ordering_keeps_stable_duplicate_identities_and_empty_input():
    first, second = ("id", "first", 0.5), ("id", "second", 0.5)
    assert rank_scored_candidates(
        [first, second], score=lambda item: item[2], identity=lambda item: (item[0],)
    ) == [first, second]
    assert rank_scored_candidates([], score=lambda item: 0.0, identity=lambda item: ()) == []


@pytest.mark.parametrize(
    "kwargs",
    [{"group": str}, {"max_per_group": 2}, {"group": str, "max_per_group": 0}],
)
def test_shared_ordering_rejects_incomplete_or_invalid_group_policy(kwargs):
    with pytest.raises(ValueError):
        rank_scored_candidates([], score=lambda item: 0.0, identity=lambda item: (), **kwargs)


def test_legacy_lexical_contract_is_shared_without_changing_tokenization():
    assert context_query_terms is query_terms
    assert _query_terms is query_terms
    assert query_terms("RAG rag GTE-3000 中文检索 ab x") == ["rag", "gte-3000", "中文检索"]
    # Legacy matching deliberately accepts substrings and Unicode case folding.
    assert term_coverage(["rag", "strasse"], "Paragraph Straße") == 1.0
    assert term_coverage([], "any text") == 0.0
