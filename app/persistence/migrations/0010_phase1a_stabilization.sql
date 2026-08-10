-- Phase 1A stabilization structure only. Existing source and backfill records
-- remain intact and no historical values are rewritten by this migration.

ALTER TABLE sources ADD COLUMN canonical_uri TEXT NOT NULL DEFAULT '';
ALTER TABLE sources ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS sources_canonical_identity_idx
    ON sources(canonical_uri, version);

ALTER TABLE knowledge_core_backfill_items ADD COLUMN item_type TEXT NOT NULL DEFAULT '';
ALTER TABLE knowledge_core_backfill_items ADD COLUMN error_message TEXT;
CREATE INDEX IF NOT EXISTS knowledge_core_backfill_items_status_idx
    ON knowledge_core_backfill_items(backfill_version, status, item_type);

CREATE TABLE IF NOT EXISTS knowledge_core_attention_items (
    id TEXT PRIMARY KEY,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'needs_attention',
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(item_type, item_id, reason)
);
CREATE INDEX IF NOT EXISTS knowledge_core_attention_status_idx
    ON knowledge_core_attention_items(status, item_type, item_id);
