-- Phase 3B Research Workflow: immutable execution choices for recovered runs.

ALTER TABLE agent_runs ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}';
