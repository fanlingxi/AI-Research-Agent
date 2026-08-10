from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan
from app.schemas.documents import DocumentChunk


def persist_evidence_chunk(
    repository: KnowledgeRepository,
    *,
    ingestion_id: str,
    evidence: EvidenceSpan,
    title: str = "Core Evidence Fixture",
    source_version: str | None = None,
) -> None:
    """Create the SQLite-first Source/Document/Chunk prerequisite for a test claim."""

    repository.add_document(
        ingestion_id=ingestion_id,
        document_id=evidence.paper_id,
        title=title,
        source="pdf",
        source_url=None,
        local_path=f"{evidence.paper_id}.pdf",
        pages=evidence.page_end,
        metadata={
            "original_source": f"{evidence.paper_id}.pdf",
            **({"source_version": source_version} if source_version else {}),
        },
    )
    repository.core_repository.record_source_document(
        document_id=evidence.paper_id,
        title=title,
        uri=f"{evidence.paper_id}.pdf",
        content=evidence.quote,
        parser_version="test-core-fixture-v1",
        metadata={
            "original_source": f"{evidence.paper_id}.pdf",
            **({"source_version": source_version} if source_version else {}),
        },
    )
    repository.core_repository.upsert_chunks(
        [
            DocumentChunk(
                id=evidence.chunk_id,
                paper_id=evidence.paper_id,
                title=title,
                text=evidence.quote,
                chunk_index=0,
                token_count=max(1, len(evidence.quote.split())),
                source_tier="primary_fulltext",
                metadata={
                    "page_start": evidence.page_start,
                    "page_end": evidence.page_end,
                },
            )
        ]
    )
