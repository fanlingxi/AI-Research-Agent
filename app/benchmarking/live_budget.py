"""Durable, fail-closed spending reservations for the explicit A02 entry point."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.llms.provider import LLMClient


class BudgetStop(RuntimeError):
    pass


class SpendingLedger:
    """Short SQLite transactions; unresolved usage is retained in every mode.

    Amounts are integer micro-CNY. Settled amounts are conservative price-based
    upper bounds, not claims about the provider's invoice or account balance.
    One persistent ledger is shared by all runs under the authorization.
    """

    @staticmethod
    def validate_policy(policy: dict) -> None:
        mode = policy.get("spending_mode", "bounded")
        unknown = policy.get("unknown_usage_policy", "stop")
        if mode not in {"bounded", "observe"} or unknown not in {"stop", "continue"}:
            raise BudgetStop("Invalid spending or unknown usage policy")
        if unknown == "continue" and mode != "observe":
            raise BudgetStop("Continuing unknown usage requires observation mode")
        positive_integers = (
            "limit_micro_cny", "max_calls", "max_input_tokens", "max_output_tokens",
            "input_cny_per_million", "output_cny_per_million", "task_deadline_seconds",
        )
        if any(type(policy.get(k)) is not int or policy[k] <= 0 for k in positive_integers):
            raise BudgetStop("Budget limits and prices must be positive integers")
        if mode == "bounded" and policy["limit_micro_cny"] > 100_000_000:
            raise BudgetStop("This authorization cannot exceed 100 CNY")
        for key, ceiling in (("context_max_tokens", 262144), ("run_token_budget", 1048576),
                             ("max_calls_per_task", 4)):
            if key in policy and (type(policy[key]) is not int or not 0 < policy[key] <= ceiling):
                raise BudgetStop(f"Invalid {key} limit")

    def __init__(self, path: Path, policy: dict):
        self.validate_policy(policy)
        self.path = path
        self.policy = json.loads(json.dumps(policy))
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, json TEXT)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, attempt TEXT, "
                "status TEXT, reserved INTEGER, charged INTEGER, details TEXT)"
            )
            serialized = json.dumps(policy, sort_keys=True)
            stored = db.execute("SELECT json FROM policy WHERE id=1").fetchone()
            if stored and stored[0] != serialized:
                raise BudgetStop("Ledger policy differs; do not reset the cumulative budget")
            db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (serialized,))
            self._ensure_history(db, serialized)

    @staticmethod
    def _ensure_history(db, original: str) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS policy_revisions (revision INTEGER PRIMARY KEY, "
            "json TEXT NOT NULL, previous_sha256 TEXT, effective_after_rowid INTEGER NOT NULL, "
            "authorization TEXT NOT NULL)"
        )
        db.execute(
            "INSERT OR IGNORE INTO policy_revisions VALUES (1, ?, NULL, 0, '{}')", (original,)
        )

    @classmethod
    def amend_policy(cls, path: Path, expected: dict, replacement: dict, *,
                     authorization: dict) -> SpendingLedger:
        """Explicit compare-and-swap amendment; retain every call and policy version.

        No calls may be pending: each revision then applies strictly after its
        recorded rowid. A repeated identical amendment is safe after a crash.
        """
        cls.validate_policy(expected)
        cls.validate_policy(replacement)
        if not all(isinstance(authorization.get(k), str) and authorization[k].strip()
                   for k in ("authorized_by", "authorized_at", "user_statement")):
            raise BudgetStop("Explicit authorization record required")
        if any(expected.get(k) != replacement.get(k) for k in ("budget_id", "currency")):
            raise BudgetStop("Amendment must retain the cumulative budget identity")
        old, new = (json.dumps(p, sort_keys=True) for p in (expected, replacement))
        approval = json.dumps(authorization, ensure_ascii=False, sort_keys=True)
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True,
                                     timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            stored = db.execute("SELECT json FROM policy WHERE id=1").fetchone()
            if stored is None or stored[0] not in (old, new):
                raise BudgetStop("Ledger policy differs from expected amendment source")
            cls._ensure_history(db, stored[0])
            latest = db.execute(
                "SELECT json,authorization FROM policy_revisions ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            if stored[0] == new:
                if latest != (new, approval):
                    raise BudgetStop("Existing amendment authorization differs")
            else:
                unresolved = db.execute(
                    "SELECT id,status FROM calls WHERE status != 'settled'"
                ).fetchall()
                acknowledged = authorization.get("acknowledged_unknown_call_ids", [])
                if unresolved and (
                    replacement.get("unknown_usage_policy", "stop") != "continue"
                    or any(status != "unknown" for _, status in unresolved)
                    or set(acknowledged) != {identifier for identifier, _ in unresolved}
                ):
                    raise BudgetStop("Pending/unknown usage blocks policy amendment")
                count, spent, last = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(charged),0),COALESCE(MAX(rowid),0) FROM calls"
                ).fetchone()
                if replacement["max_calls"] < count or (
                    replacement.get("spending_mode", "bounded") == "bounded"
                    and replacement["limit_micro_cny"] < spent
                ):
                    raise BudgetStop("New limits cannot exclude historical spending or calls")
                db.execute(
                    "INSERT INTO policy_revisions (json,previous_sha256,effective_after_rowid,"
                    "authorization) VALUES (?,?,?,?)",
                    (new, hashlib.sha256(old.encode()).hexdigest(), last, approval),
                )
                db.execute("UPDATE policy SET json=? WHERE id=1", (new,))
        return cls(path, replacement)

    def _require_current_policy(self, db) -> None:
        stored = db.execute("SELECT json FROM policy WHERE id=1").fetchone()
        if stored is None or stored[0] != json.dumps(self.policy, sort_keys=True):
            raise BudgetStop("Ledger policy differs; restart with the current policy")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def reserve(self, attempt: str, input_bound: int) -> str:
        p = self.policy
        if not 0 < input_bound <= p["max_input_tokens"]:
            raise BudgetStop("Input bound exceeds the frozen call limit")
        amount = input_bound * p["input_cny_per_million"] + (
            p["max_output_tokens"] * p["output_cny_per_million"]
        )
        call_id = uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_current_policy(db)
            states = {r[0] for r in db.execute("SELECT DISTINCT status FROM calls")}
            if "pending" in states or (
                "unknown" in states and p.get("unknown_usage_policy", "stop") == "stop"
            ):
                raise BudgetStop("Pending/unknown usage blocks further spending")
            count, spent = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls"
            ).fetchone()
            if count >= p["max_calls"] or (
                p.get("spending_mode", "bounded") == "bounded"
                and spent + amount > p["limit_micro_cny"]
            ):
                raise BudgetStop("Cumulative call or monetary limit reached")
            db.execute(
                "INSERT INTO calls VALUES (?,?, 'pending', ?, NULL, '{}')",
                (call_id, attempt, amount),
            )
        return call_id

    def settle(self, call_id: str, usage: dict | None, details: dict) -> None:
        valid = usage is not None and all(
            type(usage.get(k)) is int and usage[k] >= 0
            for k in ("input_tokens", "output_tokens")
        )
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_current_policy(db)
            row = db.execute("SELECT status,reserved FROM calls WHERE id=?", (call_id,)).fetchone()
            if row is None or row[0] != "pending":
                raise BudgetStop("Call settlement is missing or already terminal")
            charged = None
            if valid:
                charged = (
                    usage["input_tokens"] * self.policy["input_cny_per_million"]
                    + usage["output_tokens"] * self.policy["output_cny_per_million"]
                )
                input_bound = (
                    row[1] - self.policy["max_output_tokens"]
                    * self.policy["output_cny_per_million"]
                ) // self.policy["input_cny_per_million"]
                valid = (charged <= row[1] and usage["input_tokens"] <= input_bound
                         and usage["output_tokens"] <= self.policy["max_output_tokens"])
            db.execute(
                "UPDATE calls SET status=?,charged=?,details=? WHERE id=?",
                (
                    "settled" if valid else "unknown", charged if valid else None,
                    json.dumps({**details, "usage": usage}, ensure_ascii=False), call_id,
                ),
            )

    def report(self) -> dict:
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT * FROM calls ORDER BY rowid").fetchall()
            current = json.loads(db.execute("SELECT json FROM policy WHERE id=1").fetchone()[0])
            revision = db.execute("SELECT MAX(revision) FROM policy_revisions").fetchone()[0]
        return {
            "budget_id": current["budget_id"],
            "policy_revision": revision,
            "spending_mode": current.get("spending_mode", "bounded"),
            "unknown_usage_policy": current.get("unknown_usage_policy", "stop"),
            "limit_cny": (current["limit_micro_cny"] / 1_000_000
                          if current.get("spending_mode", "bounded") == "bounded" else None),
            "max_calls": current["max_calls"],
            "remaining_calls": max(0, current["max_calls"] - len(rows)),
            "remaining_unreserved_cny": (current["limit_micro_cny"] - sum(
                r[4] if r[2] == "settled" else r[3] for r in rows
            )) / 1_000_000 if current.get("spending_mode", "bounded") == "bounded" else None,
            "calls": [
                dict(id=r[0], attempt_id=r[1], status=r[2], reserved_micro_cny=r[3],
                     cost_upper_micro_cny=r[4], **json.loads(r[5])) for r in rows
            ],
            "known_cost_upper_cny": sum(r[4] or 0 for r in rows) / 1_000_000,
            "unresolved_reserved_cny": sum(
                r[3] for r in rows if r[2] != "settled"
            ) / 1_000_000,
            "billed_cost_cny": None,
            "has_unknown_usage": any(r[2] == "unknown" for r in rows),
            "stopped_for_unknown": any(
                r[2] == "pending" or (r[2] == "unknown"
                                     and current.get("unknown_usage_policy", "stop") == "stop")
                for r in rows
            ),
        }


class BudgetedLLM(LLMClient):
    provider_name = "deepseek"

    def __init__(self, delegate: LLMClient, ledger: SpendingLedger):
        self.delegate = delegate
        self.ledger = ledger
        self.last_usage: dict[str, int] = {}
        self.attempt_id = "unbound"
        self.deadline = 0.0
        self.call_count = 0
        self.audit_directory: Path | None = None

    def begin_attempt(self, attempt_id: str) -> None:
        self.attempt_id = attempt_id
        self.call_count = 0
        self.deadline = time.monotonic() + self.ledger.policy["task_deadline_seconds"]

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.last_usage = {}
        if (time.monotonic() >= self.deadline
                or self.call_count >= self.ledger.policy.get("max_calls_per_task", 4)):
            raise BudgetStop("Per-task deadline or call limit reached")
        # Byte-level conservative bound, plus framing allowance. This entry
        # only sends two plain-text messages: no tools, images or hidden schema.
        input_bound = len(prompt.encode("utf-8")) + len((system_prompt or "").encode()) + 1024
        call_id = self.ledger.reserve(self.attempt_id, input_bound)
        self.call_count += 1
        started = time.monotonic()
        try:
            text = self.delegate.invoke(prompt, system_prompt=system_prompt)
        except BaseException as exc:
            self.ledger.settle(call_id, None, {
                "error_type": type(exc).__name__, "latency_seconds": time.monotonic() - started,
                "failure_category": (
                    "cancelled" if isinstance(exc, (KeyboardInterrupt, SystemExit))
                    else "timeout" if "Timeout" in type(exc).__name__ else "exception"
                ),
            })
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            # Raw provider exceptions can contain credentials or response bodies.
            raise BudgetStop(f"Provider failed ({type(exc).__name__}); usage unknown") from None
        usage = self.delegate.last_usage if getattr(
            self.delegate, "last_usage_complete", False
        ) else None
        self.ledger.settle(call_id, usage, {
            "latency_seconds": time.monotonic() - started,
            "response_model": getattr(self.delegate, "last_response_model", None),
            "response_sha256": hashlib.sha256(text.encode()).hexdigest(),
        })
        current = next(c for c in self.ledger.report()["calls"] if c["id"] == call_id)
        if current["status"] != "settled":
            raise BudgetStop("Provider usage missing or exceeds reservation")
        self.last_usage = dict(usage)
        if self.audit_directory is not None:
            self.audit_directory.mkdir(parents=True, exist_ok=True)
            # Final content only; never persist private reasoning_content.
            (self.audit_directory / f"{call_id}.txt").write_text(text, encoding="utf-8")
        return text
