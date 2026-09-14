"""Platform-owned bounded generation records; no response text enters checkpoints."""

from __future__ import annotations

import json

from app.agent.errors import AgentRunConflictError


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
            "research_v2", "research_v3", "research_v4", "research_v5", "research_v6", "research_v7"
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
            "INSERT INTO research_generation_attempts(run_id,slot,prompt_sha256,status) "
            "VALUES(?,?,?,'pending')",
            (run_id, slot, digest),
        )
        return {"status": "pending", "created": True}


def finish(service, run_id, slot, *, response, usage, error_type, prompt_tokens):
    repo = service.repository
    with repo._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        repo._require_execution_fence_tx(db, run_id, service._current_execution.get())
        run = repo._require_run_tx(db, run_id)
        repo._require_not_terminal(run)
        changed = db.execute(
            "UPDATE research_generation_attempts SET status=?,response=?,usage_json=?,error_type=? "
            "WHERE run_id=? AND slot=? AND status='pending'",
            (
                "failed" if error_type else "returned",
                response,
                json.dumps(usage),
                error_type,
                run_id,
                slot,
            ),
        ).rowcount
        if changed != 1:
            raise AgentRunConflictError("Generation attempt already settled or missing")
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
        used = db.execute(
            "SELECT COALESCE(SUM(COALESCE(json_extract(token_usage_json, "
            "'$.input_tokens'),0)+COALESCE(json_extract(token_usage_json, "
            "'$.output_tokens'),0)),0) FROM agent_run_events WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    # Preserve actual response and use even if it exceeds the run's estimate.
    if used > run["token_budget"]:
        raise AgentRunConflictError("AgentRun has exhausted its token budget")
