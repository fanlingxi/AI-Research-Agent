ALTER TABLE sources ADD COLUMN superseded_by_source_id TEXT REFERENCES sources(id);
ALTER TABLE sources ADD COLUMN superseded_at TEXT;
CREATE INDEX idx_sources_superseded ON sources(superseded_by_source_id);

-- Freeze the local parse before dispatch, so a worker retry uses the same input.
ALTER TABLE ingestions ADD COLUMN reparse_payload_json TEXT;
