import json

import httpx
import openai
import pytest
from langchain_openai.chat_models.base import OpenAIConnectionError

from app.context.models import ContextBuildRequest, canonical_package_sha256
from app.context.service import ContextBuilderService, ContextConflictError
from app.llms.provider import LangChainChatClient, MockLLMClient
from app.retrieval.coverage import CoreCoverageReranker, CoverageReranker
from app.retrieval.policy import is_selection_transient_error
from app.retrieval.reranking import EvidenceReranker, input_digest
from app.workspace.service import _snapshot_summary
from tests.test_context_neighbors import _setup

RANKERS = [EvidenceReranker, CoverageReranker, CoreCoverageReranker]
PAYLOAD = {
    "question": "Explain methods",
    "candidates": [{"index": 0, "passages": ["Exact supporting passage for the method."]}],
}
USAGE = {"input_tokens": 12, "output_tokens": 3}


class FakeClock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class ScriptedSelection:
    timeout = 90

    def __init__(self, steps, clock, *, durations=None, usages=None):
        self.steps = steps
        self.clock = clock
        self.durations = durations or [1] * len(steps)
        self.usages = usages or [None] * len(steps)
        self.calls = 0
        self.last_usage = dict(USAGE)
        self.last_usage_complete = True

    def invoke(self, prompt, system_prompt=None):
        index = self.calls
        self.calls += 1
        self.clock.now += self.durations[index]
        usage = self.usages[index]
        if usage is not None:
            self.last_usage = usage
            self.last_usage_complete = True
        step = self.steps[index]
        if isinstance(step, Exception):
            raise step
        return step(prompt) if callable(step) else step


def response_for(ranker, payload=PAYLOAD):
    if ranker is EvidenceReranker:
        return json.dumps({"indices": [0]})
    return json.dumps({"parts": [{
        "question_part": payload["question"],
        "anchors": [{"index": 0, "quote": payload["candidates"][0]["passages"][0]}],
    }]})


@pytest.fixture
def package(tmp_path):
    repo, task, _ = _setup(
        tmp_path, parts=[f"Needle supporting method {i}. " * 5 for i in range(4)],
    )
    return ContextBuilderService(repo).preview_context(
        ContextBuildRequest(task_id=task.id, max_tokens=16000)
    )


def summarize(package, record):
    package = package.model_copy(deep=True)
    package.retrieval_audit.parameters["reranking"] = record
    return _snapshot_summary(package, persisted=True).evidence_selection


@pytest.mark.parametrize("ranker", RANKERS)
def test_transient_failure_then_success_is_audited(ranker, package):
    clock = FakeClock()
    llm = ScriptedSelection(
        [OpenAIConnectionError(request=httpx.Request("POST", "https://example.test")),
         response_for(ranker)], clock, usages=[None, USAGE],
    )
    result = ranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["status"] == "ranked"
    assert result["model_calls"] == llm.calls == 2
    assert result["retry_policy"] == "selection-call-v1" and result["max_attempts"] == 2
    assert result["input_sha256"] == input_digest(PAYLOAD)
    assert result["usage"] == USAGE and result["usage_complete"] is False
    assert result["attempts"] == [
        {"attempt": 1, "outcome": "transient_failure", "error_type": "OpenAIConnectionError",
         "latency_ms": 1000, "usage": None},
        {"attempt": 2, "outcome": "returned", "error_type": None,
         "latency_ms": 1000, "usage": USAGE},
    ]
    assert clock.sleeps == [2]
    summary = summarize(package, result)
    assert summary.status == "ranked" and summary.retried and summary.model_calls == 2
    assert summary.reason_category is None


@pytest.mark.parametrize("ranker", RANKERS)
def test_exhausted_transient_errors_fall_back_without_leaking_messages(ranker, package):
    clock = FakeClock()
    llm = ScriptedSelection([TimeoutError("SECRET"), ConnectionError("SECRET")], clock)
    result = ranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["status"] == "fallback" and result["indices"] == []
    assert result["model_calls"] == 2 and len(result["attempts"]) == 2
    assert result["usage"] is None and result["usage_complete"] is False
    assert "SECRET" not in json.dumps(result)
    assert summarize(package, result).reason_category == "transient_failure"


@pytest.mark.parametrize("ranker", RANKERS)
@pytest.mark.parametrize("response", ["not json", "{}", "[]", '{"indices":[true]}'])
def test_invalid_output_is_not_retried(ranker, response, package):
    clock = FakeClock()
    llm = ScriptedSelection([response], clock, usages=[USAGE])
    result = ranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["status"] == "fallback" and result["model_calls"] == llm.calls == 1
    assert result["attempts"][0]["outcome"] == "returned"
    assert result["usage"] == USAGE and result["usage_complete"] is True
    assert not clock.sleeps
    assert summarize(package, result).reason_category == "invalid_output"


@pytest.mark.parametrize("ranker", RANKERS)
@pytest.mark.parametrize("elapsed,timeout,expected", [(28, 90, 2), (29, 90, 1), (89, 30, 1)])
def test_retry_requires_time_for_full_client_timeout(ranker, elapsed, timeout, expected):
    clock = FakeClock()
    llm = ScriptedSelection(
        [TimeoutError(), response_for(ranker)], clock, durations=[elapsed, timeout],
    )
    llm.timeout = timeout
    result = ranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["model_calls"] == llm.calls == expected
    assert len(clock.sleeps) == expected - 1 and clock.now <= 120


def test_backoff_delay_cannot_start_a_call_outside_remaining_budget():
    clock = FakeClock()
    llm = ScriptedSelection([TimeoutError()], clock)
    result = EvidenceReranker(llm, sleep=lambda _: clock.sleep(31), clock=clock).rank(PAYLOAD)
    assert result["model_calls"] == 1 and result["reason"] == "TimeoutError"


@pytest.mark.parametrize("usages,total,complete", [
    ([USAGE, USAGE], {"input_tokens": 24, "output_tokens": 6}, True),
    ([USAGE, None], USAGE, False),
    ([None, USAGE], USAGE, False),
    ([None, None], None, False),
    ([{"input_tokens": 0, "output_tokens": 0}] * 2,
     {"input_tokens": 0, "output_tokens": 0}, True),
])
def test_usage_aggregates_only_known_attempts(usages, total, complete):
    clock = FakeClock()
    llm = ScriptedSelection([TimeoutError(), response_for(EvidenceReranker)], clock, usages=usages)
    result = EvidenceReranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["usage"] == total and result["usage_complete"] is complete
    assert [attempt["usage"] for attempt in result["attempts"]] == usages


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 429, 500, 502, 503, 599])
def test_only_rate_limit_and_server_status_errors_are_transient(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    error = openai.APIStatusError("SECRET", response=response, body=None)
    assert is_selection_transient_error(error) is (status == 429 or 500 <= status < 600)


@pytest.mark.parametrize("error", [ValueError("timeout"), TypeError("connection"),
                                  RuntimeError("429 503"), ImportError("missing SDK")])
def test_non_transient_invoke_errors_are_not_retried(error):
    clock = FakeClock()
    llm = ScriptedSelection([error], clock)
    result = EvidenceReranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["model_calls"] == 1 and not clock.sleeps
    assert result["attempts"][0]["outcome"] == "failure"


@pytest.mark.parametrize("ranker", RANKERS)
@pytest.mark.parametrize("empty", [False, True])
def test_unavailable_selection_has_no_model_attempts(ranker, empty):
    clock = FakeClock()
    llm = ScriptedSelection([], clock) if empty else MockLLMClient()
    payload = {**PAYLOAD, "candidates": []} if empty else PAYLOAD
    result = ranker(llm, sleep=clock.sleep, clock=clock).rank(payload)
    assert result["reason"] == "unavailable_or_empty"
    assert result["model_calls"] == 0 and result["attempts"] == [] and not clock.sleeps


@pytest.mark.parametrize("ranker", [CoverageReranker, CoreCoverageReranker])
def test_no_anchored_support_does_not_retry(ranker, package):
    clock = FakeClock()
    llm = ScriptedSelection([
        json.dumps({"parts": [{"question_part": PAYLOAD["question"], "anchors": []}]})
    ], clock)
    result = ranker(llm, sleep=clock.sleep, clock=clock).rank(PAYLOAD)
    assert result["reason"] == "no_anchored_support" and result["model_calls"] == 1
    assert not clock.sleeps
    assert summarize(package, result).reason_category == "no_anchored_support"


def test_production_client_disables_sdk_retries_and_bounds_timeout():
    client = LangChainChatClient("openai", "model", "test-key", "https://example.test",
                                 timeout=300, max_retries=5)
    bounded = EvidenceReranker(client).llm
    assert bounded.timeout == 90 and bounded.max_retries == 0
    assert client.timeout == 300 and client.max_retries == 5


@pytest.mark.parametrize("ranker,strategy", [
    (EvidenceReranker, "llm-v1"), (CoverageReranker, "coverage-v1"),
    (CoreCoverageReranker, "coverage-v2"),
    (CoreCoverageReranker, "coverage-v3"),
])
def test_consistency_rebuild_keeps_retry_budget_and_actual_call_audit(tmp_path, ranker, strategy):
    repo, task, claims = _setup(tmp_path)
    clock = FakeClock()

    def success_and_withdraw(prompt):
        payload, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"candidates"'):])
        with repo.database.connect() as db:
            db.execute("UPDATE claims SET status='withdrawn' WHERE id=?", (claims[0],))
        return response_for(ranker, payload)

    llm = ScriptedSelection([ConnectionError(), success_and_withdraw], clock, usages=[None, USAGE])

    class CountingRanker(ranker):
        rank_calls = 0

        def rank(self, payload):
            self.rank_calls += 1
            return super().rank(payload)

    selector = CountingRanker(llm, sleep=clock.sleep, clock=clock)
    builder = ContextBuilderService(repo, evidence_reranker=selector, coverage_reranker=selector)
    package = builder.build_context(ContextBuildRequest(
        task_id=task.id, max_tokens=16000, evidence_reranking=strategy,
    ))
    audit = package.retrieval_audit.parameters
    record = audit["reranking"]
    assert audit["preparation_attempts"] == 2
    assert selector.rank_calls == 1 and llm.calls == record["model_calls"] == 2
    assert record["reason"] == "inputs_changed_after_rerank"
    assert record["attempts"] == record["previous_attempt"]["attempts"]
    assert record["usage"] == USAGE and record["usage_complete"] is False
    assert "parts" not in record and claims[0] not in {
        bundle.claim.claim_id for bundle in package.knowledge.claim_bundles
    }
    summary = _snapshot_summary(package, persisted=True).evidence_selection
    assert summary.reason_category == "inputs_changed" and summary.retried
    assert summary.strategy == strategy
    assert canonical_package_sha256(builder.snapshot_repository.get(package.snapshot_id)) == (
        package.package_sha256
    )


@pytest.mark.parametrize("binding", ["digest", "coverage"])
@pytest.mark.parametrize("strategy", ["coverage-v1", "coverage-v3"])
def test_invalid_injected_binding_is_rejected_without_reranking(tmp_path, binding, strategy):
    repo, task, _ = _setup(tmp_path)

    class InvalidRanker:
        calls = 0

        def rank(self, payload):
            self.calls += 1
            return {"status": "ranked", "indices": [0], "parts": [],
                    "input_sha256": "wrong" if binding == "digest" else input_digest(payload)}

    ranker = InvalidRanker()
    builder = ContextBuilderService(repo, coverage_reranker=ranker)
    with pytest.raises((ContextConflictError, ValueError)):
        builder.build_context(ContextBuildRequest(
            task_id=task.id, evidence_reranking=strategy,
        ))
    assert ranker.calls == 1


@pytest.mark.parametrize("reason,category", [
    ("OpenAIConnectionError", "transient_failure"), ("APITimeoutError", "transient_failure"),
    ("RateLimitError", "transient_failure"), ("InternalServerError", "transient_failure"),
    ("OpenAIAPIError", "transient_failure"), ("ValidationError", "invalid_output"),
    ("JSONDecodeError", "invalid_output"), ("ValueError", "invalid_output"),
    ("TypeError", "invalid_output"), ("unavailable_or_empty", "unavailable"),
    ("no_anchored_support", "no_anchored_support"),
    ("inputs_changed_after_rerank", "inputs_changed"), ("AuthenticationError", "other"),
    ("APIStatusError", "other"),
])
def test_legacy_reason_categories_and_default_strategy(package, reason, category):
    summary = summarize(package, {"status": "fallback", "reason": reason, "model_calls": 1})
    assert summary.reason_category == category and summary.strategy == "llm-v1"
    assert summary.model_calls == 1 and not summary.retried


def test_summary_off_missing_audit_and_distinct_selected_sources(package):
    assert _snapshot_summary(package, persisted=False).evidence_selection is None
    assert len(package.knowledge.claim_bundles) >= 3
    assert _snapshot_summary(package, persisted=False).item_counts["knowledge_sources"] == 1
    package.knowledge.claim_bundles[0].evidence[0].source_id = "another-source"
    assert _snapshot_summary(package, persisted=False).item_counts["knowledge_sources"] == 2
    # Selection off leaves no reranking record in the audit.
    package.retrieval_audit.parameters.pop("reranking", None)
    assert _snapshot_summary(package, persisted=False).evidence_selection is None
    package.retrieval_audit = None
    assert _snapshot_summary(package, persisted=False).evidence_selection is None
    package.knowledge.claim_bundles = []
    assert _snapshot_summary(package, persisted=False).item_counts["knowledge_sources"] == 0


def test_legacy_summary_is_read_only_and_preserves_stored_hash(tmp_path):
    repo, task, _ = _setup(tmp_path)
    builder = ContextBuilderService(repo)
    package = builder.preview_context(ContextBuildRequest(task_id=task.id, max_tokens=16000))
    package.retrieval_audit.parameters["reranking"] = {
        "status": "fallback", "reason": "OpenAIConnectionError", "model_calls": 1,
        "usage": None,
    }
    package.package_sha256 = canonical_package_sha256(package)
    builder.snapshot_repository.save(package, [])
    before = package.model_dump(mode="json")
    summary = _snapshot_summary(package, persisted=True)
    assert summary.evidence_selection.reason_category == "transient_failure"
    assert package.model_dump(mode="json") == before
    stored = builder.snapshot_repository.get(package.snapshot_id)
    assert stored.model_dump(mode="json") == before
    assert canonical_package_sha256(stored) == summary.package_sha256
