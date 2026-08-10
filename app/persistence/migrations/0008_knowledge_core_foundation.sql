-- Phase 1A structural migration only.
-- Historical v1-v7 rows are intentionally not copied here.  The explicit v0009
-- data backfill performs that work after Qdrant availability has been checked.

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    uri TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(uri, version)
);

ALTER TABLE documents ADD COLUMN source_id TEXT REFERENCES sources(id);
ALTER TABLE documents ADD COLUMN content TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN content_status TEXT NOT NULL DEFAULT 'legacy_pending';
ALTER TABLE documents ADD COLUMN parser_version TEXT NOT NULL DEFAULT '';
ALTER TABLE documents ADD COLUMN parsed_at TEXT;

CREATE INDEX IF NOT EXISTS documents_source_idx ON documents(source_id);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    legacy_chunk_id TEXT UNIQUE,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    location_json TEXT NOT NULL DEFAULT '{}',
    embedding_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_document_idx ON chunks(document_id, chunk_index);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    legacy_id TEXT UNIQUE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    properties_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entities_name_idx ON entities(normalized_name, entity_type);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    legacy_id TEXT UNIQUE,
    source_entity_id TEXT NOT NULL REFERENCES entities(id),
    target_entity_id TEXT NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    properties_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS relations_endpoints_idx
    ON relations(source_entity_id, target_entity_id, relation_type);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    legacy_id TEXT UNIQUE,
    entity_id TEXT REFERENCES entities(id),
    relation_id TEXT REFERENCES relations(id),
    subject TEXT NOT NULL DEFAULT '',
    predicate TEXT NOT NULL DEFAULT '',
    object_value TEXT NOT NULL DEFAULT '',
    claim_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    confidence REAL,
    status TEXT NOT NULL,
    properties_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (entity_id IS NOT NULL AND relation_id IS NULL)
        OR (entity_id IS NULL AND relation_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS claims_entity_idx ON claims(entity_id, status);
CREATE INDEX IF NOT EXISTS claims_relation_idx ON claims(relation_id, status);

CREATE TABLE IF NOT EXISTS evidences (
    id TEXT PRIMARY KEY,
    source_id TEXT REFERENCES sources(id),
    chunk_id TEXT REFERENCES chunks(id),
    quote TEXT NOT NULL,
    quote_sha256 TEXT NOT NULL,
    location_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(chunk_id, quote_sha256)
);

CREATE TABLE IF NOT EXISTS claim_evidence_links (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    evidence_id TEXT NOT NULL REFERENCES evidences(id),
    support_role TEXT NOT NULL DEFAULT 'supports',
    created_at TEXT NOT NULL,
    PRIMARY KEY(claim_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS legacy_record_map (
    legacy_table TEXT NOT NULL,
    legacy_id TEXT NOT NULL,
    core_table TEXT NOT NULL,
    core_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(legacy_table, legacy_id),
    UNIQUE(core_table, core_id)
);

CREATE TABLE IF NOT EXISTS knowledge_core_backfills (
    version INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_core_backfill_items (
    backfill_version INTEGER NOT NULL REFERENCES knowledge_core_backfills(version),
    item_kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(backfill_version, item_kind, item_id)
);
