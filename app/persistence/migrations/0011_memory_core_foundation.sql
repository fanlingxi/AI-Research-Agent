-- Phase 1B Memory Core Foundation is additive and stores no Knowledge Core facts.

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'completed', 'archived')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_status_idx ON projects(status, updated_at);

CREATE TABLE IF NOT EXISTS workspace_tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (
        status IN ('backlog', 'ready', 'in_progress', 'blocked', 'completed', 'cancelled')
    ),
    priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workspace_tasks_project_idx
    ON workspace_tasks(project_id, status, priority, updated_at);

CREATE TABLE IF NOT EXISTS memory_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES workspace_tasks(id) ON DELETE RESTRICT,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('proposed', 'accepted', 'superseded', 'rejected')),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_decisions_project_idx
    ON memory_decisions(project_id, task_id, status, updated_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES workspace_tasks(id) ON DELETE RESTRICT,
    artifact_type TEXT NOT NULL,
    reference TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('draft', 'ready', 'superseded', 'archived')),
    supersedes_artifact_id TEXT REFERENCES artifacts(id) ON DELETE RESTRICT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_project_idx
    ON artifacts(project_id, task_id, artifact_type, status, updated_at);
CREATE INDEX IF NOT EXISTS artifacts_supersedes_idx ON artifacts(supersedes_artifact_id);

CREATE TABLE IF NOT EXISTS project_knowledge_scopes (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    collection_slug TEXT NOT NULL REFERENCES knowledge_collections(slug) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, collection_slug)
);
CREATE INDEX IF NOT EXISTS project_knowledge_scopes_collection_idx
    ON project_knowledge_scopes(collection_slug);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES workspace_tasks(id) ON DELETE RESTRICT,
    proposal_type TEXT NOT NULL CHECK (
        proposal_type IN (
            'decision_create', 'artifact_create', 'workspace_task_create',
            'project_update', 'workspace_task_update'
        )
    ),
    payload_json TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'approved', 'rejected', 'committed', 'cancelled')
    ),
    review_note TEXT,
    committed_record_type TEXT,
    committed_record_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    committed_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (
        (status != 'committed')
        OR (committed_record_type IS NOT NULL AND committed_record_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS memory_proposals_project_idx
    ON memory_proposals(project_id, task_id, status, updated_at);
