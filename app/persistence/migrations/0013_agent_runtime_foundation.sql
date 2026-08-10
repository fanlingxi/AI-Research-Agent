-- Phase 3A Agent Runtime Foundation: additive durable run audit state only.

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES workspace_tasks(id) ON DELETE RESTRICT,
    context_snapshot_id TEXT NOT NULL REFERENCES context_snapshots(id) ON DELETE RESTRICT,
    context_sha256 TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'created', 'queued', 'preparing', 'running', 'validating', 'needs_review',
            'completed', 'failed', 'cancelled', 'stale_context'
        )
    ),
    model_provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    max_steps INTEGER NOT NULL CHECK (max_steps >= 1),
    max_tool_calls INTEGER NOT NULL CHECK (max_tool_calls >= 0),
    token_budget INTEGER NOT NULL CHECK (token_budget >= 1),
    tool_call_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_call_count >= 0),
    repair_count INTEGER NOT NULL DEFAULT 0 CHECK (repair_count >= 0),
    current_node TEXT,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)
);
CREATE INDEX IF NOT EXISTS agent_runs_project_task_idx
    ON agent_runs(project_id, task_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_one_active_task_idx
    ON agent_runs(task_id)
    WHERE status IN ('created', 'queued', 'preparing', 'running', 'validating');

CREATE TABLE IF NOT EXISTS agent_run_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    node_name TEXT,
    status TEXT NOT NULL,
    input_summary_json TEXT NOT NULL DEFAULT '{}',
    output_summary_json TEXT NOT NULL DEFAULT '{}',
    token_usage_json TEXT NOT NULL DEFAULT '{}',
    latency_ms REAL,
    error_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
CREATE INDEX IF NOT EXISTS agent_run_events_run_idx
    ON agent_run_events(run_id, sequence);

CREATE TABLE IF NOT EXISTS agent_tool_calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES agent_run_events(id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    tool_name TEXT NOT NULL,
    permission TEXT NOT NULL,
    arguments_json TEXT NOT NULL DEFAULT '{}',
    result_summary_json TEXT NOT NULL DEFAULT '{}',
    result_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    idempotency_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS agent_tool_calls_run_idx
    ON agent_tool_calls(run_id, sequence);

CREATE TABLE IF NOT EXISTS agent_run_outputs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(id) ON DELETE CASCADE,
    output_type TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    output_sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
