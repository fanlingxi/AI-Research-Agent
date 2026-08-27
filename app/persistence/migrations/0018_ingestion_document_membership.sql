CREATE TABLE IF NOT EXISTS ingestion_documents (
    ingestion_id TEXT NOT NULL REFERENCES ingestions(id),
    document_id TEXT NOT NULL REFERENCES documents(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (ingestion_id, document_id)
)
;
CREATE INDEX IF NOT EXISTS ingestion_documents_document_idx
    ON ingestion_documents(document_id, ingestion_id)
;
INSERT OR IGNORE INTO ingestion_documents (ingestion_id, document_id, created_at)
SELECT ingestion_id, id, COALESCE(parsed_at, '1970-01-01T00:00:00+00:00')
FROM documents
;
