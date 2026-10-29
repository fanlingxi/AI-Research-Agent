ALTER TABLE research_generation_attempts ADD COLUMN execution_identity_json TEXT;
ALTER TABLE research_generation_attempts ADD COLUMN audit_only INTEGER NOT NULL DEFAULT 0
    CHECK(audit_only IN (0, 1));
