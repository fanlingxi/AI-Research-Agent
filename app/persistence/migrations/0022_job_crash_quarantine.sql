-- Historical attempts include successful/manual executions, so they cannot be
-- used to infer crashes. Existing jobs start with a fresh recovery budget.
ALTER TABLE knowledge_jobs ADD COLUMN crash_recoveries INTEGER NOT NULL DEFAULT 0
    CHECK (crash_recoveries >= 0);
