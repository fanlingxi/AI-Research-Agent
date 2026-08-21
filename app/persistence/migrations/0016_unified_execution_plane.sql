ALTER TABLE knowledge_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE knowledge_jobs ADD COLUMN lease_owner TEXT;
CREATE INDEX IF NOT EXISTS knowledge_jobs_claim_idx
    ON knowledge_jobs(status, priority DESC, created_at, id);
ALTER TABLE projection_outbox ADD COLUMN lease_owner TEXT;
CREATE TABLE IF NOT EXISTS executor_heartbeats (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    current_job_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS executor_heartbeats_role_idx
    ON executor_heartbeats(role, last_heartbeat_at DESC);
