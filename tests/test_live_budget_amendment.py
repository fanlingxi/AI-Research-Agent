"""Offline migration, accounting and stale-process regression coverage."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from app.benchmarking.live_budget import BudgetStop, SpendingLedger

POLICY = Path(__file__).parent / "fixtures/evaluation-policy/a02-budget-v2/run-policy.json"


@pytest.fixture
def policies():
    old = json.loads((POLICY.parent.parent / "a02/run-policy.json").read_text(encoding="utf-8"))
    new = json.loads(POLICY.read_text(encoding="utf-8"))
    approval = json.loads((POLICY.parent / "authorization.json").read_text(encoding="utf-8"))
    return old, new, approval


def legacy_ledger(path, policy):
    # Deliberately construct the old two-table schema; do not pre-migrate it.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE policy (id INTEGER PRIMARY KEY, json TEXT)")
        db.execute("INSERT INTO policy VALUES (1,?)", (json.dumps(policy, sort_keys=True),))
        db.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, attempt TEXT, status TEXT, "
                   "reserved INTEGER, charged INTEGER, details TEXT)")
        db.execute("INSERT INTO calls VALUES ('old-failure','pilot','settled',50000,32000,"
                   "'{\"failure_category\":\"output_contract\"}')")


def test_amend_legacy_preserves_calls_and_versions_and_is_idempotent(tmp_path, policies):
    old, new, approval = policies
    path = tmp_path / "ledger.db"
    legacy_ledger(path, old)
    with closing(sqlite3.connect(path)) as db:
        before = db.execute("SELECT * FROM calls").fetchall()
    ledger = SpendingLedger.amend_policy(path, old, new, authorization=approval)
    SpendingLedger.amend_policy(path, old, new, authorization=approval)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM calls").fetchall() == before
        history = db.execute("SELECT json,effective_after_rowid FROM policy_revisions "
                             "ORDER BY revision").fetchall()
    assert [(json.loads(p), rowid) for p, rowid in history] == [(old, 0), (new, 1)]
    assert ledger.report()["limit_cny"] == 100
    assert ledger.report()["known_cost_upper_cny"] == 0.032
    assert ledger.report()["remaining_unreserved_cny"] == 99.968
    call = ledger.reserve("larger-output", 20000)
    ledger.settle(call, {"input_tokens": 20000, "output_tokens": 16384}, {})
    assert ledger.report()["known_cost_upper_cny"] == 0.203072
    assert ledger.report()["remaining_calls"] == 1998
    assert ledger.report()["policy_revision"] == 2


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_amendment_cannot_release_unresolved_reservations(tmp_path, policies, state):
    old, new, approval = policies
    ledger = SpendingLedger(tmp_path / "ledger.db", old)
    call = ledger.reserve("interrupted", 100)
    if state == "unknown":
        ledger.settle(call, None, {})
    before = ledger.report()
    with pytest.raises(BudgetStop, match="Pending/unknown"):
        SpendingLedger.amend_policy(ledger.path, old, new, authorization=approval)
    assert ledger.report() == before


def test_stale_process_cannot_spend_or_settle_under_previous_rates(tmp_path, policies):
    old, new, approval = policies
    stale = SpendingLedger(tmp_path / "ledger.db", old)
    current = SpendingLedger.amend_policy(stale.path, old, new, authorization=approval)
    with pytest.raises(BudgetStop, match="policy differs"):
        stale.reserve("stale", 100)
    call = current.reserve("current", 100)
    with pytest.raises(BudgetStop, match="policy differs"):
        stale.settle(call, {"input_tokens": 50, "output_tokens": 10}, {})
    current.settle(call, {"input_tokens": 50, "output_tokens": 10}, {})
    assert stale.report()["limit_cny"] == 100


@pytest.mark.parametrize("fault", ["identity", "authorization", "source", "limit"])
def test_invalid_amendment_leaves_policy_and_history_unchanged(tmp_path, policies, fault):
    old, new, approval = policies
    ledger = SpendingLedger(tmp_path / "ledger.db", old)
    if fault == "identity":
        new["budget_id"] = "reset-history"
    elif fault == "authorization":
        approval = {}
    elif fault == "source":
        old["max_calls"] += 1
    else:
        new["limit_micro_cny"] = 100_000_001
    with pytest.raises(BudgetStop):
        SpendingLedger.amend_policy(ledger.path, old, new, authorization=approval)
    assert ledger.report()["policy_revision"] == 1
    assert ledger.report()["limit_cny"] == 20


def test_reservation_and_amendment_are_serialized(tmp_path, policies):
    old, new, approval = policies
    ledger = SpendingLedger(tmp_path / "ledger.db", old)

    def reserve():
        try:
            ledger.reserve("racing", 100)
            return "reserved"
        except BudgetStop:
            return "blocked"

    def amend():
        try:
            SpendingLedger.amend_policy(ledger.path, old, new, authorization=approval)
            return "amended"
        except BudgetStop:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(reserve), pool.submit(amend)
        result = (first.result(), second.result())
    assert result in (("reserved", "blocked"), ("blocked", "amended"))


def test_raised_budget_still_enforces_cumulative_spend(tmp_path, policies):
    old, new, approval = policies
    path = tmp_path / "ledger.db"
    legacy_ledger(path, old)
    ledger = SpendingLedger.amend_policy(path, old, new, authorization=approval)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE calls SET charged=99900000")
    with pytest.raises(BudgetStop, match="monetary"):
        ledger.reserve("cannot-afford-maximum-output", 20000)


def test_observation_amendment_retains_unknown_and_allows_further_calls(tmp_path, policies):
    old, new, approval = policies
    ledger = SpendingLedger(tmp_path / "ledger.db", old)
    unknown = ledger.reserve("old-timeout", 100)
    ledger.settle(unknown, None, {"error_type": "TimeoutError"})
    before = ledger.report()["calls"]
    new = {**new, "spending_mode": "observe", "unknown_usage_policy": "continue",
           "limit_micro_cny": 1, "run_token_budget": 1048576, "context_max_tokens": 262144}
    with pytest.raises(BudgetStop, match="Pending/unknown"):
        SpendingLedger.amend_policy(ledger.path, old, new, authorization=approval)
    approval = {**approval, "acknowledged_unknown_call_ids": [unknown]}
    current = SpendingLedger.amend_policy(ledger.path, old, new, authorization=approval)
    SpendingLedger.amend_policy(ledger.path, old, new, authorization=approval)
    assert current.report()["calls"] == before
    assert current.report()["has_unknown_usage"]
    assert not current.report()["stopped_for_unknown"]
    assert current.report()["limit_cny"] is None
    assert current.report()["remaining_unreserved_cny"] is None
    with pytest.raises(BudgetStop, match="policy differs"):
        ledger.reserve("stale", 100)
    call = current.reserve("new-call", 20000)
    with pytest.raises(BudgetStop, match="Pending/unknown"):
        current.reserve("concurrent", 100)
    current.settle(call, {"input_tokens": 10000, "output_tokens": 1000}, {})
    assert current.report()["calls"][0] == before[0]
    assert current.report()["known_cost_upper_cny"] == 0.028
    assert current.report()["unresolved_reserved_cny"] > 0


def test_observation_cannot_amend_while_call_is_pending(tmp_path, policies):
    old, new, approval = policies
    ledger = SpendingLedger(tmp_path / "ledger.db", old)
    call = ledger.reserve("in-flight", 100)
    new = {**new, "spending_mode": "observe", "unknown_usage_policy": "continue"}
    with pytest.raises(BudgetStop, match="Pending/unknown"):
        SpendingLedger.amend_policy(ledger.path, old, new, authorization={
            **approval, "acknowledged_unknown_call_ids": [call]})
    assert ledger.report()["policy_revision"] == 1
