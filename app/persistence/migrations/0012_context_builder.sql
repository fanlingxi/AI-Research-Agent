-- Phase 2 Context Builder stores immutable, traceable runtime context packages.
-- These rows are an audit read model. They do not alter Knowledge or Memory facts.

CREATE TABLE IF NOT EXISTS context_snapshots (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL REFERENCES workspace_tasks(id) ON DELETE RESTRICT,
    project_revision INTEGER NOT NULL CHECK (project_revision >= 1),
    task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
    builder_version TEXT NOT NULL,
    package_schema_version TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    package_json TEXT NOT NULL,
    package_sha256 TEXT NOT NULL,
    token_budget INTEGER NOT NULL CHECK (token_budget > 0),
    used_tokens INTEGER NOT NULL CHECK (used_tokens >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS context_snapshots_task_idx
    ON context_snapshots(project_id, task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS context_snapshots_fingerprint_idx
    ON context_snapshots(project_id, task_id, request_fingerprint);

CREATE TABLE IF NOT EXISTS context_snapshot_items (
    snapshot_id TEXT NOT NULL REFERENCES context_snapshots(id) ON DELETE CASCADE,
    section TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    parent_item_id TEXT,
    rank INTEGER,
    score REAL,
    selected_reason TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    estimated_tokens INTEGER NOT NULL DEFAULT 0 CHECK (estimated_tokens >= 0),
    PRIMARY KEY(snapshot_id, section, item_type, item_id)
);
CREATE INDEX IF NOT EXISTS context_snapshot_items_lookup_idx
    ON context_snapshot_items(item_type, item_id);
