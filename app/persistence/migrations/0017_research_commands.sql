CREATE TABLE IF NOT EXISTS research_commands (
    id TEXT PRIMARY KEY,
    idempotency_key_hash TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('quick_report', 'project_run')),
    status TEXT NOT NULL CHECK(status IN (
        'accepted', 'preparing', 'target_created', 'queued', 'completed', 'failed'
    )),
    instruction TEXT NOT NULL,
    request_json TEXT NOT NULL,
    orchestration_stage TEXT NOT NULL,
    project_id TEXT,
    task_id TEXT,
    snapshot_id TEXT,
    target_resource_type TEXT,
    target_resource_id TEXT,
    target_route TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_commands_status_idx
    ON research_commands(status, updated_at DESC, id DESC);
