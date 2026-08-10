from __future__ import annotations

import json
from types import SimpleNamespace

from app.config.settings import Settings
from app.knowledge.backfills.v0009_knowledge_core_backfill import (
    run_v0009_knowledge_core_backfill,
)
from app.knowledge.core_repository import KnowledgeCoreRepository
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan, PublishedEntity, PublishedRelation


def _legacy_records(repository: KnowledgeRepository):
    ingestion = repository.create_ingestion(
        topic="v8 回填", sources=["fixture.pdf"], pdf_max_pages=2, enqueue=False
    )
    repository.add_document(
        ingestion_id=ingestion.id,
        document_id="paper:v8",
        title="Legacy Core Paper",
        source="pdf",
        source_url=None,
        local_path="fixture.pdf",
        pages=1,
        metadata={"original_source": "fixture.pdf"},
    )
    evidence = EvidenceSpan(
        paper_id="paper:v8",
        chunk_id="paper:v8:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Legacy Qdrant payload remains the first backfill source.",
    )
    source = PublishedEntity(
        id="legacy-entity-source",
        name="Legacy Source",
        type="Paper",
        summary="一篇用于验证 v8 知识核心回填的历史论文。",
        evidence=[evidence],
    )
    target = PublishedEntity(
        id="legacy-entity-target",
        name="Legacy Method",
        type="Method",
        summary="一种具有可复核原文证据的历史研究方法。",
        evidence=[evidence],
    )
    relation = PublishedRelation(
        id="legacy-relation",
        source_entity_id=source.id,
        target_entity_id=target.id,
        type="PRESENTS",
        summary="论文提出了该历史研究方法。",
        confidence=0.9,
        evidence=[evidence],
    )
    with repository._connect() as connection:
        now = "2026-08-06T00:00:00+00:00"
        for entity in (source, target):
            connection.execute(
                """
                INSERT INTO published_entities (
                    id, normalized_name, entity_type, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.id,
                    entity.name.casefold(),
                    entity.type,
                    entity.model_dump_json(),
                    now,
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO published_relations (
                id, source_entity_id, target_entity_id, relation_type, payload_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relation.id,
                relation.source_entity_id,
                relation.target_entity_id,
                relation.type,
                relation.model_dump_json(),
                now,
                now,
            ),
        )
    return source, target, relation


def test_v8_is_structural_and_does_not_run_data_backfill_at_repository_startup(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))

    assert repository.schema_version() == 15
    with repository._connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "sources",
            "chunks",
            "entities",
            "relations",
            "claims",
            "evidences",
            "claim_evidence_links",
            "legacy_record_map",
            "knowledge_core_backfills",
            "projects",
            "workspace_tasks",
            "memory_decisions",
            "artifacts",
            "project_knowledge_scopes",
            "memory_proposals",
        }.issubset(tables)
        backfill_count = connection.execute(
            "SELECT COUNT(*) FROM knowledge_core_backfills"
        ).fetchone()[0]
        assert backfill_count == 0


def test_v0009_backfill_uses_qdrant_payloads_and_generates_new_core_ids(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    source, target, relation = _legacy_records(repository)
    core = KnowledgeCoreRepository(repository.path)
    payload = {
        "id": "paper:v8:page:1:chunk:0",
        "paper_id": "paper:v8",
        "title": "Legacy Core Paper",
        "text": "Legacy Qdrant payload remains the first backfill source.",
        "chunk_index": 0,
        "token_count": 8,
        "source_tier": "primary_fulltext",
        "metadata": {"page_start": 1, "page_end": 1},
    }

    first = core.backfill_legacy_documents([payload])
    second = core.backfill_legacy_documents([payload])

    with repository._connect() as connection:
        source_map = connection.execute(
            """
            SELECT core_id FROM legacy_record_map
            WHERE legacy_table = 'published_entities' AND legacy_id = ?
            """,
            (source.id,),
        ).fetchone()
        target_map = connection.execute(
            """
            SELECT core_id FROM legacy_record_map
            WHERE legacy_table = 'published_entities' AND legacy_id = ?
            """,
            (target.id,),
        ).fetchone()
        relation_map = connection.execute(
            """
            SELECT core_id FROM legacy_record_map
            WHERE legacy_table = 'published_relations' AND legacy_id = ?
            """,
            (relation.id,),
        ).fetchone()
        chunk = connection.execute(
            "SELECT content, page_start, page_end FROM chunks WHERE id = ?",
            (payload["id"],),
        ).fetchone()
        backfill = connection.execute(
            "SELECT status, summary_json FROM knowledge_core_backfills WHERE version = 9"
        ).fetchone()

    assert source_map is not None and source_map["core_id"] != source.id
    assert target_map is not None and target_map["core_id"] != target.id
    assert relation_map is not None and relation_map["core_id"] != relation.id
    assert chunk is not None
    assert chunk["content"] == payload["text"]
    assert (chunk["page_start"], chunk["page_end"]) == (1, 1)
    assert first.entities == second.entities
    assert first.relations == second.relations
    assert core.is_v0009_backfill_ready()
    assert core.published_paper_ids() == {"paper:v8"}
    assert backfill["status"] == "completed"
    assert json.loads(backfill["summary_json"])["chunks"] == 1


def test_v0009_reads_qdrant_payloads_before_writing_core_rows(tmp_path, monkeypatch) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    _legacy_records(repository)
    payload = {
        "id": "paper:v8:page:1:chunk:0",
        "paper_id": "paper:v8",
        "title": "Legacy Core Paper",
        "text": "Legacy Qdrant payload remains the first backfill source.",
        "chunk_index": 0,
        "token_count": 8,
        "source_tier": "primary_fulltext",
        "metadata": {"page_start": 1, "page_end": 1},
    }
    calls: list[tuple[str, object]] = []

    class _QdrantClient:
        def __init__(self, *, url: str) -> None:
            calls.append(("init", url))

        def collection_exists(self, collection_name: str) -> bool:
            calls.append(("exists", collection_name))
            return True

        def scroll(self, **kwargs):
            calls.append(("scroll", kwargs["collection_name"]))
            return [SimpleNamespace(payload=payload)], None

    monkeypatch.setattr("qdrant_client.QdrantClient", _QdrantClient)

    summary = run_v0009_knowledge_core_backfill(
        KnowledgeCoreRepository(repository.path),
        settings=Settings(qdrant_url="http://qdrant.test", knowledge_qdrant_collection="legacy"),
    )

    assert [name for name, _ in calls] == ["init", "exists", "scroll"]
    assert summary.chunks == 1
