import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.knowledge import build_knowledge_router
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.map_view import KnowledgeMapReader
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateDecision, ChunkSearchHit
from app.knowledge.source_versions import SourceVersionRepository
from app.worker import KnowledgeWorker
from tests.core_fixtures import SQLiteReportQuery
from tests.test_context_builder import _project_with_task, _VectorCandidates
from tests.test_knowledge_workflow import _parser, _service


def _setup(tmp_path, monkeypatch, *, uri=None):
    repository, service, _ = _service(tmp_path)
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"offline parser fixture")
    old_parse = _parser(source=str(path), max_pages=4)
    # The fixture text must match its chunks, just as the real PDF parser does.
    old_parse.text = "\n\n".join(old_parse.page_texts)
    monkeypatch.setattr(service, "parser", lambda **kwargs: old_parse)
    monkeypatch.setattr("app.knowledge.service.PARSER_VERSION", "pypdf-v1")
    ingestion = service.submit(collection="版本测试", sources=[uri or str(path)], pdf_max_pages=4)
    assert service.run(ingestion.id).status == "needs_review"
    _publish(service, ingestion.id)
    versions = SourceVersionRepository(repository.database)
    old = versions.documents()[0]
    monkeypatch.setattr("app.knowledge.service.PARSER_VERSION", "pypdf-v2")
    reports = KnowledgeReportService(repository, settings=service.settings, require_live_llm=False)
    app = FastAPI()
    app.include_router(build_knowledge_router(repository, service, reports))
    worker = KnowledgeWorker(repository, ingestion_service=service, report_service=reports)
    return repository, service, versions, old, old_parse, TestClient(app), worker


def _publish(service, ingestion_id):
    for kind in ("entity", "relation"):
        for item in service.repository.list_candidates(ingestion_id, status="draft"):
            if item["kind"] != kind:
                continue
            candidate = item["candidate"]
            existing = next((entity for entity in service.repository.list_published_entities()
                             if kind == "entity" and entity.name == candidate.get("name")), None)
            decision = CandidateDecision(decision="merge", canonical_id=existing.id) if existing \
                else CandidateDecision(decision="approve")
            service.decide(candidate["id"], decision)
    service.drain_projections()


def _new_parse(old_parse):
    return old_parse.model_copy(update={
        "title": "修复后的论文", "text": old_parse.text + "\nCorrected version text.",
        "page_texts": [*old_parse.page_texts[:-1],
                       old_parse.page_texts[-1] + " Corrected version text."],
        "metadata": {"etag": "unchanged-pdf-bytes"},
    })


def test_activation_preview_counts_only_disappearing_knowledge(tmp_path, monkeypatch):
    repository, service, versions, old, parsed, client, worker = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "parser", lambda **kwargs: _new_parse(parsed).model_copy(
        update={"title": parsed.title},
    ))
    ingestion_id = service.reparse(old["document_id"])["ingestion_id"]
    assert worker.run_once()
    _publish(service, ingestion_id)
    new = next(item for item in versions.documents() if item["source_id"] != old["source_id"])
    # Retain one entity and one relation with old-only evidence. Other shared
    # knowledge keeps both versions' evidence and must not be counted.
    with repository.database.connect() as connection:
        for kind in ("entity", "relation"):
            item_id = connection.execute(f"""
                SELECT c.{kind}_id FROM claims c
                JOIN claim_evidence_links l ON l.claim_id = c.id
                JOIN evidences e ON e.id = l.evidence_id
                WHERE c.{kind}_id IS NOT NULL AND c.status = 'published'
                GROUP BY c.{kind}_id HAVING COUNT(DISTINCT e.source_id) = 2
                ORDER BY c.{kind}_id LIMIT 1
            """).fetchone()[0]
            connection.execute(f"""
                DELETE FROM claim_evidence_links WHERE claim_id IN
                (SELECT id FROM claims WHERE {kind}_id = ?)
                AND evidence_id IN (SELECT id FROM evidences WHERE source_id = ?)
            """, (item_id, new["source_id"]))
        before = "\n".join(connection.iterdump())
    response = client.get(f"/api/knowledge/sources/{new['source_id']}/activation-preview")
    assert response.status_code == 200
    preview = response.json()
    assert preview["entity_count"] == 1
    assert preview["relation_count"] == 1
    assert preview["replaced_version_count"] == 1
    assert len(preview["entity_samples"]) == 1
    assert len(preview["relation_samples"]) == 1
    with repository.database.connect() as connection:
        assert "\n".join(connection.iterdump()) == before
    versions.activate(new["source_id"])
    assert versions.activation_preview(new["source_id"])["entity_count"] == 0
    # On switching back, shared knowledge survives through the restored target.
    restored = versions.activation_preview(old["source_id"])
    assert restored["entity_count"] == 0
    assert restored["relation_count"] == 0
    assert client.get("/api/knowledge/sources/missing/activation-preview").status_code == 404


def test_reparse_same_content_is_noop_and_local_pdf_is_required(tmp_path, monkeypatch):
    repository, service, versions, old, parsed, client, _ = _setup(tmp_path, monkeypatch)
    with repository.database.connect() as connection:
        before = connection.execute("SELECT COUNT(*) FROM knowledge_jobs").fetchone()[0]
    result = client.post(f"/api/knowledge/documents/{old['document_id']}/reparse")
    assert result.status_code == 200
    assert result.json()["status"] == "unchanged"
    assert result.json()["ingestion_id"] is None
    assert versions.documents()[0]["parser_version"] == "pypdf-v1"
    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_jobs").fetchone()[0] == before
        connection.execute("UPDATE documents SET local_path = 'missing.pdf'")
    assert client.post(f"/api/knowledge/documents/{old['document_id']}/reparse").status_code == 422
    with repository.database.connect() as connection:
        connection.execute("UPDATE documents SET local_path = ?", (str(tmp_path / "file.txt"),))
    (tmp_path / "file.txt").write_text("text")
    assert client.post(f"/api/knowledge/documents/{old['document_id']}/reparse").status_code == 422
    assert client.post("/api/knowledge/documents/unknown/reparse").status_code == 404
    assert client.post("/api/knowledge/sources/unknown/activate").status_code == 404
    with repository.database.connect() as connection:
        connection.execute("UPDATE documents SET local_path = ?", (str(tmp_path / "paper.pdf"),))

    def broken_parser(**kwargs):
        raise OSError("bad file")

    monkeypatch.setattr(service, "parser", broken_parser)
    response = client.post(f"/api/knowledge/documents/{old['document_id']}/reparse")
    assert response.status_code == 422
    assert "PDF 解析失败" in response.json()["detail"]


def test_reparse_queues_frozen_input_and_is_concurrently_idempotent(tmp_path, monkeypatch):
    repository, service, versions, old, parsed, client, worker = _setup(
        tmp_path, monkeypatch, uri="https://example.org/paper.pdf",
    )
    monkeypatch.setattr(service, "parser", lambda **kwargs: _new_parse(parsed))
    with repository.database.connect() as connection:
        old_document = dict(connection.execute("SELECT * FROM documents").fetchone())
        old_source = dict(connection.execute("SELECT * FROM sources").fetchone())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: service.reparse(old["document_id"]), range(2)))
    assert responses[0]["ingestion_id"] == responses[1]["ingestion_id"]
    assert sorted(item["deduplicated"] for item in responses) == [False, True]
    ingestion_id = responses[0]["ingestion_id"]
    assert repository.get_ingestion(ingestion_id).collection_slug == repository.get_ingestion(
        old["ingestion_id"]
    ).collection_slug
    monkeypatch.setattr(service, "parser", lambda **kwargs: pytest.fail("must use saved parse"))
    assert worker.run_once()
    assert repository.get_ingestion(ingestion_id).status == "needs_review"
    new = next(item for item in versions.documents() if item["document_id"] != old["document_id"])
    assert new["parser_version"] == "pypdf-v2"
    assert new["canonical_uri"] == old["canonical_uri"] == "https://example.org/paper.pdf"
    assert new["version"] != old["version"]
    assert new["version"].startswith("content-sha256:")
    assert not new["has_published_evidence"]
    assert all(item["candidate"]["status"] == "draft"
               for item in repository.list_candidates(ingestion_id))
    assert client.post(f"/api/knowledge/sources/{new['source_id']}/activate").status_code == 409
    with repository.database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM documents WHERE id = ?",
                                       (old["document_id"],)).fetchone()) == old_document
        assert dict(connection.execute("SELECT * FROM sources WHERE id = ?",
                                       (old["source_id"],)).fetchone()) == old_source
    monkeypatch.setattr(service, "parser", lambda **kwargs: _new_parse(parsed))
    assert service.reparse(old["document_id"])["ingestion_id"] == ingestion_id
    _publish(service, ingestion_id)
    assert service.reparse(old["document_id"])["ingestion_id"] == ingestion_id
    # Parsing back to an existing version reuses it rather than relabelling v1.
    monkeypatch.setattr(service, "parser", lambda **kwargs: parsed)
    reverted = client.post(f"/api/knowledge/documents/{new['document_id']}/reparse")
    assert reverted.status_code == 202
    assert reverted.json()["ingestion_id"] == old["ingestion_id"]
    assert len(versions.documents()) == 2


def test_activation_filters_live_reads_and_preserves_frozen_snapshot(tmp_path, monkeypatch):
    repository, service, versions, old, parsed, client, worker = _setup(tmp_path, monkeypatch)
    scope = repository.get_ingestion(old["ingestion_id"]).collection_slug
    project, task, _, _ = _project_with_task(repository, scope)
    with repository.database.connect() as connection:
        old_chunk = connection.execute("SELECT id FROM chunks ORDER BY id").fetchone()[0]
    builder = ContextBuilderService(repository, vector_retriever=_VectorCandidates(old_chunk))
    request = ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=16000,
                                  enable_vector_candidates=True)
    frozen = builder.build(request)
    assert frozen.knowledge.claim_bundles
    with repository.database.connect() as connection:
        frozen_rows = [tuple(row) for row in connection.execute("SELECT * FROM context_snapshots")]
    monkeypatch.setattr(service, "parser", lambda **kwargs: _new_parse(parsed))
    ingestion_id = service.reparse(old["document_id"])["ingestion_id"]
    assert worker.run_once()
    _publish(service, ingestion_id)
    new = next(item for item in versions.documents() if item["document_id"] != old["document_id"])
    assert client.post(f"/api/knowledge/sources/{new['source_id']}/activate").status_code == 200
    assert client.post(f"/api/knowledge/sources/{new['source_id']}/activate").status_code == 200
    listed = client.get("/api/knowledge/documents").json()
    assert {item["source_id"] for item in listed if item["is_active"]} == {new["source_id"]}
    assert all(len(item["versions"]) == 2 for item in listed)
    library = client.get(f"/api/knowledge/collections/{scope}").json()
    assert {e["paper_id"] for item in library["entities"] + library["relations"]
            for e in item["evidence"]} == {new["document_id"]}
    package = builder.build(request)
    assert package.knowledge.claim_bundles
    assert {ev.source_id for bundle in package.knowledge.claim_bundles for ev in bundle.evidence} \
        == {new["source_id"]}
    with repository.database.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM context_snapshots WHERE id = ?",
                                        (frozen.snapshot_id,)).fetchone()) == frozen_rows[0]
    assert (builder.snapshot_repository.get(frozen.snapshot_id).package_sha256
            == frozen.package_sha256)
    # Both vectors are returned by the offline projection, including the old ID.
    search = SQLiteReportQuery(repository).search("agent tools", topic_slugs=[scope])
    assert {item["paper_id"] for item in search["evidence"]} == {new["document_id"]}
    assert repository.core_repository.rehydrate_report_evidence(
        [ChunkSearchHit(chunk_id=old_chunk, score=1)], allowed_paper_ids={old["document_id"]},
    ) == []
    graph = KnowledgeMapReader(repository.database)
    current_map = graph.graph()
    assert "Readable Agent Paper" not in {node.label for node in current_map.nodes}
    for node in current_map.nodes:
        detail = graph.node_detail(node.id)
        assert {evidence["document_id"] for claim in detail["claims"]
                for evidence in claim["evidence"]} <= {new["document_id"]}
    with pytest.raises(KeyError):
        graph.node_detail(f"source:{old['document_id']}")
    assert client.post(f"/api/knowledge/sources/{old['source_id']}/activate").status_code == 200
    restored = builder.build(request)
    assert {ev.source_id for bundle in restored.knowledge.claim_bundles for ev in bundle.evidence} \
        == {old["source_id"]}
    search = SQLiteReportQuery(repository).search("agent tools", topic_slugs=[scope])
    assert {item["paper_id"] for item in search["evidence"]} == {old["document_id"]}
    assert "修复后的论文" not in {node.label for node in graph.graph().nodes}
    assert builder.snapshot_repository.get(frozen.snapshot_id).model_dump() == frozen.model_dump()


def test_activation_during_vector_io_is_revalidated(tmp_path, monkeypatch):
    repository, service, versions, old, parsed, _, worker = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(service, "parser", lambda **kwargs: _new_parse(parsed))
    ingestion_id = service.reparse(old["document_id"])["ingestion_id"]
    assert worker.run_once()
    _publish(service, ingestion_id)
    new = next(item for item in versions.documents() if item["source_id"] != old["source_id"])
    query = SQLiteReportQuery(repository)
    original_search = query.chunk_search.search
    calls = []

    def search(*args, **kwargs):
        calls.append(kwargs["allowed_paper_ids"])
        if len(calls) == 1:
            versions.activate(new["source_id"])
        return original_search(*args, **kwargs)

    monkeypatch.setattr(query.chunk_search, "search", search)
    result = query.search("agent tools")
    assert len(calls) == 2
    assert {item["paper_id"] for item in result["evidence"]} == {new["document_id"]}


def test_reparse_rechecks_collection_after_parsing(tmp_path, monkeypatch):
    repository, service, _, old, parsed, _, _ = _setup(tmp_path, monkeypatch)

    def parser(**kwargs):
        repository.move_ingestion_collection(old["ingestion_id"], "另一个集合")
        return _new_parse(parsed)

    monkeypatch.setattr(service, "parser", parser)
    with pytest.raises(ValueError, match="所属集合已变化"):
        service.reparse(old["document_id"])
    assert len(repository.list_ingestions()) == 1


def test_source_version_migration_is_additive_and_repeatable(tmp_path, monkeypatch):
    path = str(tmp_path / "pre-version.db")
    original = KnowledgeRepository._apply_structural_migration
    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeRepository, "_apply_structural_migration",
                      lambda self, version: original(self, version) if version < 21 else None)
        KnowledgeRepository(path)
    with sqlite3.connect(path) as connection:
        connection.execute("""INSERT INTO sources
            (id, source_type, title, uri, canonical_uri, version, content_sha256,
             metadata_json, created_at, updated_at)
            VALUES ('legacy', 'pdf', 'Old', 'old.pdf', 'old.pdf', 'v1', 'hash', '{}', 't', 't')""")
    for _ in range(2):
        assert KnowledgeRepository(path).schema_version() == 23
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT version, content_sha256, superseded_by_source_id, superseded_at FROM sources"
        ).fetchone() == ("v1", "hash", None, None)
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 21"
        ).fetchone()[0] == 1
