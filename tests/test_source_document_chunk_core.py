from app.knowledge.core_repository import KnowledgeCoreRepository
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan
from app.schemas.documents import DocumentChunk


def test_new_ingestion_content_is_persisted_in_sqlite_before_projection(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    ingestion = repository.create_ingestion(
        topic="Core Source", sources=["fixture.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.add_document(
        ingestion_id=ingestion.id,
        document_id="paper:core",
        title="Core Source Paper",
        source="pdf",
        source_url=None,
        local_path="fixture.pdf",
        pages=1,
        metadata={"original_source": "fixture.pdf"},
    )
    chunk = DocumentChunk(
        id="paper:core:page:1:chunk:0",
        paper_id="paper:core",
        title="Core Source Paper",
        text="A SQLite-first knowledge core persists source text before vector indexing.",
        chunk_index=0,
        token_count=10,
        source_tier="primary_fulltext",
        metadata={"page_start": 1, "page_end": 1},
    )

    core = KnowledgeCoreRepository(repository.path)
    core.record_source_document(
        document_id="paper:core",
        title="Core Source Paper",
        uri="fixture.pdf",
        content=chunk.text,
        parser_version="test-parser-v1",
        metadata={"original_source": "fixture.pdf"},
    )
    assert core.upsert_chunks([chunk]) == 1

    candidate = CandidateEntity(
        id="candidate-core",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.topic_slug,
        name="SQLite-first Knowledge Core",
        type="Concept",
        summary="将来源文本、分块和正式事实保存在 SQLite 的知识核心设计。",
        confidence=0.9,
        evidence=EvidenceSpan(
            paper_id="paper:core",
            chunk_id=chunk.id,
            page_start=1,
            page_end=1,
            quote="A SQLite-first knowledge core persists source text before vector indexing.",
        ),
    )
    repository.add_candidate_entity(candidate)
    legacy_entity = repository.publish_entity(candidate.id)

    with repository._connect() as connection:
        document = connection.execute(
            "SELECT source_id, content, content_status FROM documents WHERE id = 'paper:core'"
        ).fetchone()
        mapped = connection.execute(
            """
            SELECT core_id FROM legacy_record_map
            WHERE legacy_table = 'published_entities' AND legacy_id = ?
            """,
            (legacy_entity.id,),
        ).fetchone()
        evidence_link_count = connection.execute(
            "SELECT COUNT(*) FROM claim_evidence_links"
        ).fetchone()[0]

    assert document["source_id"]
    assert document["content"] == chunk.text
    assert document["content_status"] == "available"
    assert mapped is not None and mapped["core_id"] != legacy_entity.id
    assert evidence_link_count == 1
