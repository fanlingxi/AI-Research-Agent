"""Platform-owned bounded generation records; no response text enters checkpoints."""

from __future__ import annotations

import json

from app.agent.errors import (
    AgentRunConflictError,
    AgentRunExecutionRevokedError,
    AgentRunTerminalError,
)


def begin(service, run_id, slot, digest):
    if type(slot) is not int or slot not in (0, 1):
        raise ValueError("Only two generation attempts are permitted")
    repo = service.repository
    with repo._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        repo._require_execution_fence_tx(db, run_id, service._current_execution.get())
        run = repo._require_run_tx(db, run_id)
        repo._require_not_terminal(run)
        if run["plugin_workflow_key"] not in {
            "research_v2", "research_v3", "research_v4", "research_v5", "research_v6",
            "research_v7", "research_v8", "research_v9", "research_v10", "research_v11",
        }:
            raise AgentRunConflictError("Generation records require a structured research workflow")
        row = db.execute(
            "SELECT * FROM research_generation_attempts WHERE run_id=? AND slot=?", (run_id, slot)
        ).fetchone()
        if row:
            if row["prompt_sha256"] != digest:
                raise AgentRunConflictError("Generation input changed on recovery")
            return {**dict(row), "created": False}
        db.execute(
            "INSERT INTO research_generation_attempts"
            "(run_id,slot,prompt_sha256,status,execution_identity_json) "
            "VALUES(?,?,?,'pending',?)",
            (run_id, slot, digest, json.dumps(_execution_identity(service, db, run_id))),
        )
        return {"status": "pending", "created": True}


def _execution_identity(service, db, run_id):
    fence = service._current_execution.get()
    job = db.execute(
        "SELECT id,attempts,lease_owner,status FROM knowledge_jobs "
        "WHERE kind='agent_run' AND resource_id=?", (run_id,)
    ).fetchone()
    return {
        "fence": vars(fence) if fence else None,
        "job": dict(job) if job else None,
    }


def finish(service, run_id, slot, *, digest, response, usage, error_type, prompt_tokens):
    """Settle one issued call; False forbids further business work by this executor.

    Cancellation/revocation may clear a lease, but a later claim increments attempts.
    Both the persisted issuing identity and that counter must still match. Legacy
    pending rows have no identity and cannot use this relaxed settlement path.
    """
    repo = service.repository
    with repo._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        run = repo._require_run_tx(db, run_id)
        row = db.execute(
            "SELECT * FROM research_generation_attempts WHERE run_id=? AND slot=?", (run_id, slot)
        ).fetchone()
        if row is None or row["prompt_sha256"] != digest or not row["execution_identity_json"]:
            raise AgentRunConflictError("Generation settlement does not match an issued attempt")
        issued = json.loads(row["execution_identity_json"])
        current = _execution_identity(service, db, run_id)
        if issued["fence"] != current["fence"]:
            raise AgentRunExecutionRevokedError("Generation settlement executor changed")
        old_job, job = issued["job"], current["job"]
        if old_job != job and (
            old_job is None or job is None
            or old_job["id"] != job["id"] or old_job["attempts"] != job["attempts"]
            or job["lease_owner"] is not None
        ):
            raise AgentRunExecutionRevokedError(
                "Generation settlement was superseded by a new claim"
            )
        cancelled = run["status"] == "cancelled"
        audit_only = cancelled or (
            job is not None and (
                job["status"] not in {"queued", "running"}
                or (old_job["status"] == "running" and job["status"] != "running")
            )
        )
        try:
            repo._require_execution_fence_tx(db, run_id, service._current_execution.get())
        except AgentRunConflictError:
            audit_only = True
        if row["status"] != "pending":
            # First result wins, including its usage; retries cannot overwrite it.
            try:
                repo._require_not_terminal(run)
            except AgentRunTerminalError:
                return False
            if audit_only or row["audit_only"]:
                return False
            totals, _ = repo._trace_totals_tx(db, run_id)
            if totals["total_tokens"] > run["token_budget"]:
                raise AgentRunConflictError("AgentRun has exhausted its token budget")
            return True
        if not cancelled:
            repo._require_not_terminal(run)
        db.execute(
            "UPDATE research_generation_attempts "
            "SET status=?,response=?,usage_json=?,error_type=?,audit_only=? "
            "WHERE run_id=? AND slot=? AND status='pending'",
            (
                "failed" if error_type else "returned",
                response,
                json.dumps(usage),
                error_type,
                int(audit_only),
                run_id,
                slot,
            ),
        )
        if audit_only:
            # This transaction changes only the attempt ledger, never events or outputs.
            return False
        repo._append_event_tx(
            db,
            run_id=run_id,
            event_type="node_completed",
            node_name="research_draft" if slot == 0 else "repair",
            status=str(run["status"]),
            output_summary={
                "attempt_reference": f"research-attempt:{run_id}:{slot}",
                "usage_known": usage is not None,
                "error_type": error_type,
                "full_prompt_estimated_tokens": prompt_tokens,
            },
            token_usage=usage or {},
        )
        totals, _ = repo._trace_totals_tx(db, run_id)
    # Preserve actual response and use even if it exceeds the run's estimate.
    if totals["total_tokens"] > run["token_budget"]:
        raise AgentRunConflictError("AgentRun has exhausted its token budget")
    return True
