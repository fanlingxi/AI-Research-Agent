from app.knowledge.query import KnowledgeQueryService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import ChunkSearchHit, EvidenceSpan
from app.schemas.documents import DocumentChunk


class SQLiteReportQuery(KnowledgeQueryService):
    """Replace external projections only; retain production hydration and commit inputs."""

    def __init__(self, repository):
        class Chunks:
            def search(self, query, **kwargs):
                with repository.database.connect() as connection:
                    rows = connection.execute("SELECT id FROM chunks ORDER BY id").fetchall()
                return [ChunkSearchHit(chunk_id=row[0], score=0.94) for row in rows]

        class Graph:
            def search(self, query, **kwargs):
                return []

        super().__init__(repository, chunk_search=Chunks(), graph_search=Graph())


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
