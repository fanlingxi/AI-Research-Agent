CREATE TABLE research_generation_attempts (
    run_id TEXT NOT NULL REFERENCES agent_runs(id),
    slot INTEGER NOT NULL CHECK(slot IN (0, 1)),
    prompt_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'returned', 'failed')),
    response TEXT,
    usage_json TEXT,
    error_type TEXT,
    PRIMARY KEY (run_id, slot)
);
