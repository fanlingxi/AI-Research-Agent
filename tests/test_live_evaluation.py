"""A02 remains offline here: transport doubles, real production services."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.benchmarking.fixtures import ScriptedResearchLLM
from app.benchmarking.live import (
    DATASET,
    execute_task,
    isolated_settings,
    seeded_repository,
    span_coverage,
    summarize,
)
from app.benchmarking.live_budget import BudgetedLLM, BudgetStop, SpendingLedger
from app.benchmarking.live_dataset import approved_dataset, import_papers, task_input
from app.config.settings import Settings
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LangChainChatClient
from app.worker import KnowledgeWorker

POLICY = Path(__file__).parent / "fixtures/evaluation-policy/a02-budget-v2/run-policy.json"
APPROVAL = POLICY.parent.parent / "a02/papers-approval.json"


@pytest.fixture
def policy():
    # Preserve regression coverage for the frozen S0 policy.
    return json.loads((POLICY.parent.parent / "a02/run-policy.json").read_text(encoding="utf-8"))


def test_approval_binds_frozen_candidate(tmp_path):
    corpus, tasks, approval = approved_dataset(DATASET, APPROVAL)
    assert len(tasks) == 40 and len(corpus.sources) == 8
    assert all(t.annotation.status == "pending_human" for t in tasks)
    approval["freeze_sha256"] = "0" * 64
    path = tmp_path / "wrong.json"
    path.write_text(json.dumps(approval), encoding="utf-8")
    with pytest.raises(ValueError, match="Approval"):
        approved_dataset(DATASET, path)


def test_gold_input_allowlist():
    _, tasks, _ = approved_dataset(DATASET, APPROVAL)
    for task in tasks:
        assert set(task_input(task)) == {
            "id", "question", "report_requirements", "allowed_source_ids"
        }


def test_ledger_reopen_and_budget_exhaustion(tmp_path, policy):
    policy["limit_micro_cny"] = 100_000
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    call = ledger.reserve("first", 100)
    ledger.settle(call, {"input_tokens": 100, "output_tokens": 4000}, {})
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    with pytest.raises(BudgetStop, match="monetary"):
        ledger.reserve("second", 40000)
    assert ledger.report()["known_cost_upper_cny"] == 0.0322


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": -1, "output_tokens": 2},
                                  {"input_tokens": True, "output_tokens": 2},
                                  {"input_tokens": 10000000, "output_tokens": 2}])
def test_unknown_or_invalid_usage_blocks_restarts(tmp_path, policy, usage):
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    call = ledger.reserve("test", 100)
    ledger.settle(call, usage, {"error_type": "synthetic"})
    restarted = SpendingLedger(tmp_path / "ledger.db", policy)
    assert restarted.report()["stopped_for_unknown"]
    assert restarted.report()["billed_cost_cny"] is None
    with pytest.raises(BudgetStop, match="unknown"):
        restarted.reserve("retry", 100)


def test_pending_crash_and_double_settlement(tmp_path, policy):
    path = tmp_path / "ledger.db"
    ledger = SpendingLedger(path, policy)
    call = ledger.reserve("crashed", 100)
    with pytest.raises(BudgetStop, match="Pending"):
        SpendingLedger(path, policy).reserve("after crash", 100)
    ledger.settle(call, {"input_tokens": 90, "output_tokens": 9}, {})
    with pytest.raises(BudgetStop, match="already terminal"):
        ledger.settle(call, {"input_tokens": 90, "output_tokens": 9}, {})


def test_atomic_reservations_and_policy_change(tmp_path, policy):
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)

    def reserve(i):
        try:
            return ledger.reserve(str(i), 100)
        except BudgetStop:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(v is not None for v in pool.map(reserve, range(4))) == 1
    with pytest.raises(BudgetStop, match="policy differs"):
        SpendingLedger(ledger.path, {**policy, "max_calls": 999})


def test_budget_wrapper_failure_does_not_leak_or_retry(tmp_path, policy):
    class Broken:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("secret-api-key")

    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    llm = BudgetedLLM(Broken(), ledger)
    llm.begin_attempt("failure")
    with pytest.raises(BudgetStop, match="usage unknown") as error:
        llm.invoke("hi", "system")
    assert "secret-api-key" not in str(error.value)
    assert "secret-api-key" not in json.dumps(ledger.report())
    assert len(ledger.report()["calls"]) == 1
    with pytest.raises(BudgetStop):
        llm.invoke("retry", "system")
    assert len(ledger.report()["calls"]) == 1


def test_call_and_input_and_time_limits(tmp_path, policy):
    policy["max_calls"] = 1
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    with pytest.raises(BudgetStop, match="Input"):
        ledger.reserve("large", policy["max_input_tokens"] + 1)
    call = ledger.reserve("one", 100)
    ledger.settle(call, {"input_tokens": 5, "output_tokens": 1}, {})
    with pytest.raises(BudgetStop, match="call or monetary"):
        ledger.reserve("two", 100)
    llm = BudgetedLLM(None, ledger)
    with pytest.raises(BudgetStop, match="deadline"):
        llm.invoke("expired", "system")


def test_all_attempt_denominators():
    statuses = ["completed", "failed", "needs_review", "cancelled", "exception", "not_attempted"]
    report = summarize([{"status": s} for s in statuses], {})
    assert report["planned_count"] == 6
    assert report["attempted_count"] == 5
    assert report["completion_rate_over_planned"] == 1 / 6
    assert report["semantic_success_rate"] is None


def test_provider_explicit_limits_and_usage_reset(monkeypatch):
    from types import SimpleNamespace

    import langchain_openai

    observed = []

    class Chat:
        def __init__(self, **kwargs):
            observed.append(kwargs)

        def invoke(self, messages):
            return SimpleNamespace(content="{}", usage_metadata=None, response_metadata={})

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", Chat)
    llm = LangChainChatClient("deepseek", "deepseek-v4-flash", "fake", "https://example.test",
                             max_tokens=4096, max_retries=0, timeout=90, thinking_enabled=False)
    llm.last_usage = {"input_tokens": 999, "output_tokens": 999}
    llm.last_usage_complete = True
    llm.invoke("hello")
    assert not llm.last_usage_complete
    assert observed[0]["max_retries"] == 0
    assert observed[0]["timeout"] == 90
    assert observed[0]["extra_body"] == {"max_tokens": 4096, "thinking": {"type": "disabled"}}


@pytest.mark.parametrize("update", [
    {"limit_micro_cny": 100_000_001}, {"limit_micro_cny": -1},
    {"input_cny_per_million": 0}, {"max_calls": True},
])
def test_invalid_budget_policy_rejected(tmp_path, policy, update):
    with pytest.raises(BudgetStop):
        SpendingLedger(tmp_path / "ledger.db", {**policy, **update})


def test_output_cap_violation_stops_even_with_spare_reservation(tmp_path, policy):
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    call = ledger.reserve("bad provider", 60000)
    ledger.settle(call, {"input_tokens": 20, "output_tokens": 4097}, {})
    assert ledger.report()["stopped_for_unknown"]


def test_seed_identity_stable_and_tamper_rejected(tmp_path, monkeypatch):
    import app.benchmarking.live as live

    monkeypatch.setattr(live, "EVALUATION_ROOT", tmp_path)
    corpus, _, approval = approved_dataset(DATASET, APPROVAL)
    paper = corpus.sources[0]
    page = paper.pages[0]
    corpus = corpus.model_copy(update={"sources": [paper.model_copy(update={"pages": [page]})]})
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _, mapping1 = seeded_repository(first, corpus, approval)
    seed = tmp_path / "paper-seed-v1/knowledge.db"
    before = hashlib.sha256(seed.read_bytes()).hexdigest()
    _, mapping2 = seeded_repository(second, corpus, approval)
    assert mapping1 == mapping2
    assert hashlib.sha256(seed.read_bytes()).hexdigest() == before
    with closing(sqlite3.connect(seed)) as db, db:
        db.execute("CREATE TABLE tampering (id TEXT)")
    third = tmp_path / "third"
    third.mkdir()
    with pytest.raises(ValueError, match="seed changed"):
        seeded_repository(third, corpus, approval)


@pytest.mark.parametrize("expanded", [False, True])
def test_production_chain_and_verbatim_scope(tmp_path, policy, expanded):
    if expanded:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
    # A reduced corpus for the behavior test; the live importer uses all pages.
    corpus, tasks, _ = approved_dataset(DATASET, APPROVAL)
    paper = corpus.sources[0]
    page = paper.pages[0]
    paper = paper.model_copy(update={"pages": [page], "text": paper.text[:page.end]})
    corpus = corpus.model_copy(update={"sources": [paper], "spans": []})
    task = tasks[0]
    settings = isolated_settings(tmp_path, Settings(_env_file=None))
    repository = KnowledgeRepository(settings.knowledge_db_path)
    mapping = import_papers(repository, corpus, DATASET)
    ranges = sorted(mapping["chunks"].values(), key=lambda i: i["start"])
    assert ranges[0]["start"] == 0 and ranges[-1]["end"] == page.end
    assert all(a["end"] == b["start"] for a, b in zip(ranges, ranges[1:], strict=False))
    class LargeUsageLLM(ScriptedResearchLLM):
        def invoke(self, *args, **kwargs):
            text = super().invoke(*args, **kwargs)
            # Three calls totaling 30k must fit the expanded run allowance.
            self.last_usage = {"input_tokens": 1000, "output_tokens": 9000}
            return text

    delegate = LargeUsageLLM() if expanded else ScriptedResearchLLM()
    delegate.last_usage_complete = True
    ledger = SpendingLedger(tmp_path / "ledger.db", policy)
    llm = BudgetedLLM(delegate, ledger)
    context = ContextBuilderService(repository)
    service = AgentRunService(repository, context_builder=context, settings=settings, llm=llm)
    runtime = AgentRuntime(service, checkpoint_factory=AgentCheckpointFactory(
        settings.agent_checkpoint_path))
    worker = KnowledgeWorker(repository, ingestion_service=None, report_service=None,
                             agent_runtime=runtime)
    # No gold spans needed by execution; evaluation uses the complete original
    # span index only after the input snapshot has already been built.
    full_corpus, _, _ = approved_dataset(DATASET, APPROVAL)
    result = execute_task(repository, context, service, worker, task, full_corpus,
                          mapping, llm, tmp_path / "attempt")
    assert result["status"] == "completed"
    assert result["semantic_success"] is None
    assert len(ledger.report()["calls"]) == 3
    assert (tmp_path / "checkpoints.db").exists()
    assert (tmp_path / "attempt/report.md").exists()
    assert not any(t.expectation.rationale in p for t in tasks for p in delegate.calls)
    assert service.repository.list_tool_calls(result["run_id"])
    assert service.get_run(result["run_id"]).token_budget == (131072 if expanded else 16000)
    if expanded:
        assert sum(sum(e.token_usage.values()) for e in service.repository.list_events(
            result["run_id"]
        )) == 30000
    assert any('"claim_bundle_id"' in p and "Required JSON schema:" in p for p in delegate.calls)
    with repository._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0
    package = service.load_context_snapshot(result["run_id"])
    assert package.token_usage.budget == (16000 if expanded else 6000)
    metrics = span_coverage(task, full_corpus, package, mapping)
    assert metrics["required_span_count"] == 1
    assert set(metrics["required_span_coverage"]) == {"r-aspects"}
