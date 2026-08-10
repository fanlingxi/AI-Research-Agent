-- Phase 5A Domain Plugin Foundation: additive routing and audit identity only.
-- Plugin implementations remain static application code and this schema never stores code.

CREATE TABLE IF NOT EXISTS project_domain_plugins (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    plugin_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('enabled', 'disabled')),
    config_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, plugin_key)
);
CREATE INDEX IF NOT EXISTS project_domain_plugins_project_idx
    ON project_domain_plugins(project_id, status, plugin_key);

-- Existing Projects retain their frozen Research behaviour through an explicit,
-- idempotent first-party binding. This does not touch Knowledge or Memory facts.
INSERT OR IGNORE INTO project_domain_plugins (
    project_id, plugin_key, status, config_json, revision, created_at, updated_at
)
SELECT id, 'research', 'enabled', '{}', 1, updated_at, updated_at FROM projects;

ALTER TABLE workspace_tasks
    ADD COLUMN domain_plugin_key TEXT NOT NULL DEFAULT 'research';
CREATE INDEX IF NOT EXISTS workspace_tasks_plugin_idx
    ON workspace_tasks(project_id, domain_plugin_key, updated_at);

ALTER TABLE agent_runs
    ADD COLUMN plugin_key TEXT NOT NULL DEFAULT 'research';
ALTER TABLE agent_runs
    ADD COLUMN plugin_version TEXT NOT NULL DEFAULT 'phase5a-v1';
ALTER TABLE agent_runs
    ADD COLUMN plugin_contract_version TEXT NOT NULL DEFAULT '1';
ALTER TABLE agent_runs
    ADD COLUMN plugin_workflow_key TEXT NOT NULL DEFAULT 'research';
CREATE INDEX IF NOT EXISTS agent_runs_plugin_idx
    ON agent_runs(plugin_key, plugin_version, plugin_workflow_key, created_at DESC);
