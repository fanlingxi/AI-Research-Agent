-- Feedback lives alongside AgentRun audit facts, with no Knowledge write path.
CREATE TABLE agent_run_feedback (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_json TEXT NOT NULL,
    anchor_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected', 'resolved')),
    decision_json TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, idempotency_key)
);
CREATE INDEX agent_run_feedback_run_idx ON agent_run_feedback(run_id, created_at, id);

CREATE TABLE agent_run_rechecks (
    id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL REFERENCES agent_run_feedback(id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL,
    request_json TEXT NOT NULL,
    parent_run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE RESTRICT,
    child_run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(id) ON DELETE RESTRICT,
    recheck_json TEXT,
    recheck_anchor_json TEXT,
    created_at TEXT NOT NULL,
    checked_at TEXT,
    UNIQUE(feedback_id, idempotency_key)
);
CREATE INDEX agent_run_rechecks_parent_idx ON agent_run_rechecks(parent_run_id);
