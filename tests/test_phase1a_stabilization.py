import pytest

from app.knowledge.core_repository import ClaimEvidenceValidationError, KnowledgeCoreRepository
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan, PublishedEntity
from app.knowledge.source_identity import (
    canonicalize_source_uri,
    derive_source_identity,
    source_document_id,
)
from app.schemas.documents import DocumentChunk
from tests.core_fixtures import persist_evidence_chunk


def _evidence(*, paper_id: str = "paper:stabilization") -> EvidenceSpan:
    quote = "A grounded claim is traceable to the SQLite chunk that supports it."
    return EvidenceSpan(
        paper_id=paper_id,
        chunk_id=f"{paper_id}:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote=quote,
    )


def _candidate(ingestion_id: str, topic_slug: str, evidence: EvidenceSpan) -> CandidateEntity:
    return CandidateEntity(
        id="candidate-stabilization",
        ingestion_id=ingestion_id,
        topic_slug=topic_slug,
        name="Grounded Core Claim",
        type="Concept",
        summary="由可定位来源分块支撑并可被审核的知识核心断言。",
        confidence=0.94,
        evidence=evidence,
    )


def _repository_with_ingestion(tmp_path) -> tuple[KnowledgeRepository, object]:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    ingestion = repository.create_ingestion(
        topic="Phase 1A Stabilization", sources=["fixture.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    return repository, ingestion


def test_claim_without_evidence_is_blocked_and_records_attention(tmp_path) -> None:
    repository, _ = _repository_with_ingestion(tmp_path)
    entity = PublishedEntity(
        id="legacy-without-evidence",
        name="Unsupported Core Entity",
        type="Concept",
        summary="不应在没有证据时成为正式知识。",
    )

    with pytest.raises(ClaimEvidenceValidationError, match="requires at least one Evidence"):
        repository.core_repository.synchronize_published_entity(entity)

    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        attention = connection.execute(
            "SELECT status FROM knowledge_core_attention_items WHERE item_id = ?",
            (entity.id,),
        ).fetchone()
    assert attention is not None and attention["status"] == "needs_attention"


def test_evidence_without_chunk_cannot_publish_legacy_or_core_fact(tmp_path) -> None:
    repository, ingestion = _repository_with_ingestion(tmp_path)
    candidate = _candidate(ingestion.id, ingestion.topic_slug, _evidence())
    repository.add_candidate_entity(candidate)

    with pytest.raises(ClaimEvidenceValidationError, match="does not exist"):
        repository.publish_entity(candidate.id)

    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM published_entities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        attention = connection.execute(
            """
            SELECT status, reason FROM knowledge_core_attention_items
            WHERE item_type = 'candidate_entity' AND item_id = ?
            """,
            (candidate.id,),
        ).fetchone()
    assert attention is not None
    assert attention["status"] == "needs_attention"
    assert "does not exist" in attention["reason"]


def test_valid_evidence_chunk_allows_claim_publication(tmp_path) -> None:
    repository, ingestion = _repository_with_ingestion(tmp_path)
    evidence = _evidence()
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    candidate = _candidate(ingestion.id, ingestion.topic_slug, evidence)
    repository.add_candidate_entity(candidate)

    repository.publish_entity(candidate.id)

    with repository._connect() as connection:
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("entities", "claims", "evidences", "claim_evidence_links")
        ]
    assert counts == [1, 1, 1, 1]


def test_reset_ingestion_deletes_chunks_before_documents_without_orphans(tmp_path) -> None:
    repository, ingestion = _repository_with_ingestion(tmp_path)
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=_evidence())

    repository.reset_ingestion(ingestion.id)

    with repository._connect() as connection:
        document_count = connection.execute(
            "SELECT COUNT(*) FROM documents WHERE ingestion_id = ?", (ingestion.id,)
        ).fetchone()[0]
        chunk_count = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert document_count == 0
    assert chunk_count == 0
    assert foreign_key_violations == []


def test_v0009_marks_attention_when_required_qdrant_chunk_is_missing(tmp_path) -> None:
    repository, ingestion = _repository_with_ingestion(tmp_path)
    evidence = _evidence(paper_id="paper:legacy-attention")
    repository.add_document(
        ingestion_id=ingestion.id,
        document_id=evidence.paper_id,
        title="Legacy Attention Paper",
        source="pdf",
        source_url=None,
        local_path="legacy-attention.pdf",
        pages=1,
        metadata={},
    )
    entity = PublishedEntity(
        id="legacy-attention-entity",
        name="Legacy Attention Entity",
        type="Paper",
        summary="历史记录必须等待可验证 Chunk 后才能进入 Core。",
        evidence=[evidence],
    )
    with repository._connect() as connection:
        connection.execute(
            """
            INSERT INTO published_entities (
                id, normalized_name, entity_type, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '2026-08-06T00:00:00+00:00', '2026-08-06T00:00:00+00:00')
            """,
            (entity.id, entity.name.casefold(), entity.type, entity.model_dump_json()),
        )

    summary = repository.core_repository.backfill_legacy_documents([])

    assert summary.status == "completed_with_attention"
    assert summary.needs_attention >= 1
    assert not repository.core_repository.is_v0009_backfill_ready()
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
        statuses = {
            row["status"]
            for row in connection.execute(
                "SELECT status FROM knowledge_core_backfill_items WHERE backfill_version = 9"
            )
        }
    assert "needs_attention" in statuses


def test_v0009_failed_checkpoint_is_retried_without_reprocessing_completed_items(
    tmp_path, monkeypatch
) -> None:
    repository, ingestion = _repository_with_ingestion(tmp_path)
    evidence = _evidence(paper_id="paper:checkpoint")
    repository.add_document(
        ingestion_id=ingestion.id,
        document_id=evidence.paper_id,
        title="Checkpoint Paper",
        source="pdf",
        source_url=None,
        local_path="checkpoint.pdf",
        pages=1,
        metadata={},
    )
    payload = DocumentChunk(
        id=evidence.chunk_id,
        paper_id=evidence.paper_id,
        title="Checkpoint Paper",
        text=evidence.quote,
        chunk_index=0,
        token_count=12,
        source_tier="primary_fulltext",
        metadata={"page_start": 1, "page_end": 1},
    ).model_dump()
    core = KnowledgeCoreRepository(repository.path)
    original_upsert = core._upsert_chunk_tx

    def fail_chunk_once(*args, **kwargs) -> None:
        raise RuntimeError("simulated checkpoint failure")

    monkeypatch.setattr(core, "_upsert_chunk_tx", fail_chunk_once)
    first = core.backfill_legacy_documents([payload])
    assert first.status == "failed"
    with repository._connect() as connection:
        failed_checkpoint = connection.execute(
            """
            SELECT status, error_message FROM knowledge_core_backfill_items
            WHERE backfill_version = 9 AND item_kind = 'chunk' AND item_id = ?
            """,
            (evidence.chunk_id,),
        ).fetchone()
    assert failed_checkpoint is not None
    assert failed_checkpoint["status"] == "failed"
    assert failed_checkpoint["error_message"] == "simulated checkpoint failure"

    monkeypatch.setattr(core, "_upsert_chunk_tx", original_upsert)
    second = core.backfill_legacy_documents([payload])
    assert second.status == "completed"
    with repository._connect() as connection:
        checkpoint = connection.execute(
            """
            SELECT item_type, status, error_message
            FROM knowledge_core_backfill_items
            WHERE backfill_version = 9 AND item_kind = 'chunk' AND item_id = ?
            """,
            (evidence.chunk_id,),
        ).fetchone()
    assert checkpoint is not None
    assert checkpoint["item_type"] == "chunk"
    assert checkpoint["status"] == "completed"
    assert checkpoint["error_message"] is None


def test_source_canonicalization_and_content_version_are_stable() -> None:
    canonical = canonicalize_source_uri(" HTTPS://Example.COM:443/a/../paper.pdf#section ")
    assert canonical == "https://example.com/paper.pdf"
    assert canonicalize_source_uri(" docs\\papers\\..\\paper.pdf ") == "docs/paper.pdf"
    first = derive_source_identity(
        uri="https://example.com/paper.pdf",
        content_sha256="a" * 64,
        metadata={},
    )
    second = derive_source_identity(
        uri=" https://EXAMPLE.com:443/paper.pdf ",
        content_sha256="a" * 64,
        metadata={},
    )
    assert first == second
    assert first.version == f"content-sha256:{'a' * 64}"
    explicit = derive_source_identity(
        uri="https://example.com/paper.pdf",
        content_sha256="b" * 64,
        metadata={"source_version": "publisher-v2"},
    )
    assert explicit.version == "publisher-v2"
    assert source_document_id(
        uri="https://example.com/paper.pdf#download",
        content_sha256="a" * 64,
        metadata={},
    ) == source_document_id(
        uri=" HTTPS://EXAMPLE.COM:443/paper.pdf ",
        content_sha256="a" * 64,
        metadata={},
    )
    assert source_document_id(
        uri="https://example.com/paper.pdf",
        content_sha256="a" * 64,
        metadata={},
    ) != source_document_id(
        uri="https://example.com/paper.pdf",
        content_sha256="b" * 64,
        metadata={},
    )


def test_same_document_version_can_belong_to_multiple_ingestions(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    first = repository.create_ingestion(
        collection="Collection A",
        sources=["paper.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    second = repository.create_ingestion(
        collection="Collection B",
        sources=["paper.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    document_id = source_document_id(
        uri="paper.pdf",
        content_sha256="a" * 64,
        metadata={},
    )
    for ingestion in (first, second):
        repository.add_document(
            ingestion_id=ingestion.id,
            document_id=document_id,
            title="Shared paper",
            source="pdf",
            source_url=None,
            local_path="paper.pdf",
            pages=2,
            metadata={"original_source": "paper.pdf"},
        )

    with repository._connect() as connection:
        owner = connection.execute(
            "SELECT ingestion_id FROM documents WHERE id = ?", (document_id,)
        ).fetchone()["ingestion_id"]
        memberships = {
            row["ingestion_id"]
            for row in connection.execute(
                "SELECT ingestion_id FROM ingestion_documents WHERE document_id = ?",
                (document_id,),
            )
        }

    assert owner == first.id
    assert memberships == {first.id, second.id}
    assert repository.get_ingestion(first.id).document_count == 1
    assert repository.get_ingestion(second.id).document_count == 1
