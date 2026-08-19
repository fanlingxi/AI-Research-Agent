from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan, ExtractedEntity, KnowledgeExtraction, PaperReading
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.schemas.documents import ParsedDocument
from app.worker import KnowledgeWorker


class _ApiExtractor:
    def extract(self, paper, chunks):
        evidence = EvidenceSpan(
            paper_id=paper.id,
            chunk_id=chunks[0].id,
            page_start=1,
            page_end=1,
            quote="Agents use tools to solve research tasks with evidence.",
        )
        return ExtractionResult(
            KnowledgeExtraction(
                reading=PaperReading(
                    research_problem="研究智能体如何基于证据调用工具完成复杂任务。",
                    core_contributions=["提供可审核知识抽取流程。"],
                    method_summary="通过工具调用和证据记录完成多步任务。",
                    evidence=evidence,
                ),
                entities=[
                    ExtractedEntity(
                        name="证据驱动智能体",
                        type="Method",
                        summary="将工具返回的可定位证据用于多步推理的智能体方法。",
                        aliases=[],
                        confidence=0.9,
                        evidence=evidence,
                    )
                ],
            )
        )


def _api_parser(*, source: str, max_pages: int) -> ParsedDocument:
    return ParsedDocument(
        source=source,
        title="API Knowledge Paper",
        text="Agents use tools to solve research tasks with evidence.",
        pages=1,
        page_texts=["Agents use tools to solve research tasks with evidence."],
        page_numbers=[1],
    )


def _stack(tmp_path):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        chunk_size=40,
        chunk_overlap=10,
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    service = KnowledgeIngestionService(
        repository,
        settings=settings,
        extractor=_ApiExtractor(),
        parser=_api_parser,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        vault_exporter=KnowledgeVaultExporter(settings.knowledge_vault_path),
        require_live_llm=False,
    )
    reports = KnowledgeReportService(repository, settings=settings, require_live_llm=False)
    worker = KnowledgeWorker(
        repository,
        ingestion_service=service,
        report_service=reports,
        lease_seconds=30,
    )
    return repository, service, reports, worker


def test_health_is_knowledge_only_and_v1_routes_are_absent(tmp_path) -> None:
    repository, service, reports, _ = _stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=service,
            report_service=reports,
        )
    ) as client:
        health = client.get("/health")
        proxied_health = client.get("/api/knowledge/health")
        old_research = client.post("/api/research", json={"query": "legacy"})
        old_task = client.get("/api/tasks/does-not-exist")

    assert health.status_code == 200
    assert proxied_health.status_code == 200
    assert proxied_health.json()["knowledge"]["schema_version"] == 15
    assert health.json()["knowledge"]["schema_version"] == 15
    assert old_research.status_code == 404
    assert old_task.status_code == 404


def test_api_accepts_150_pdf_pages_and_rejects_151(tmp_path) -> None:
    repository, service, reports, _ = _stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=service,
            report_service=reports,
        )
    ) as client:
        accepted = client.post(
            "/api/knowledge/ingestions",
            json={"sources": ["long-paper.pdf"], "pdf_max_pages": 150},
        )
        rejected = client.post(
            "/api/knowledge/ingestions",
            json={"sources": ["too-long-paper.pdf"], "pdf_max_pages": 151},
        )

    assert accepted.status_code == 202
    assert accepted.json()["pdf_max_pages"] == 150
    assert rejected.status_code == 422


def test_api_returns_bounded_candidate_review_pages(tmp_path) -> None:
    repository, service, reports, worker = _stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=service,
            report_service=reports,
        )
    ) as client:
        ingestion = client.post(
            "/api/knowledge/ingestions",
            json={"sources": ["page-fixture.pdf"], "pdf_max_pages": 2},
        ).json()
        assert worker.run_once()
        page = client.get(
            f"/api/knowledge/ingestions/{ingestion['id']}/candidate-page?limit=1&offset=0"
        )
        filtered = client.get(
            f"/api/knowledge/ingestions/{ingestion['id']}/candidate-page?kind=entity&min_confidence=0.9&limit=25"
        )

    assert page.status_code == 200
    assert page.json()["total"] > 1
    assert len(page.json()["items"]) == 1
    assert page.json()["next_offset"] == 1
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2
    assert all(item["kind"] == "entity" for item in filtered.json()["items"])


def test_api_queues_ingestion_and_worker_publishes_outbox(tmp_path) -> None:
    repository, service, reports, worker = _stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=service,
            report_service=reports,
        )
    ) as client:
        submitted = client.post(
            "/api/knowledge/ingestions",
            json={"topic": "智能体证据审核", "sources": ["api-fixture.pdf"], "pdf_max_pages": 2},
        )
        ingestion_id = submitted.json()["id"]
        queued = client.get(f"/api/knowledge/ingestions/{ingestion_id}")

        assert queued.json()["status"] == "queued"
        assert queued.json()["queued_job_count"] == 1
        assert queued.json()["job_status"] == "queued"
        assert queued.json()["queue_position"] == 1
        assert worker.run_once()

        candidates = client.get(f"/api/knowledge/ingestions/{ingestion_id}/candidates")
        approved = client.post(f"/api/knowledge/ingestions/{ingestion_id}/approve-ready")
        assert approved.json()["ingestion"]["status"] == "needs_review"
        for item in candidates.json():
            if item["kind"] == "relation":
                response = client.post(
                    f"/api/knowledge/candidates/{item['candidate']['id']}/decision",
                    json={"decision": "approve"},
                )
                assert response.status_code == 200

        while worker.run_once():
            pass
        topic = client.get("/api/knowledge/topics/智能体证据审核")
        graph = client.get("/api/knowledge/graph?topic_slug=智能体证据审核")
        completed = client.get(f"/api/knowledge/ingestions/{ingestion_id}")

    assert submitted.status_code == 202
    assert len(candidates.json()) == 3
    assert completed.json()["status"] == "completed"
    assert topic.status_code == 200
    assert len(graph.json()["nodes"]) == 2
    assert len(graph.json()["edges"]) == 1


def test_api_reuses_an_equivalent_active_ingestion_submission(tmp_path) -> None:
    repository, service, reports, _ = _stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=service,
            report_service=reports,
        )
    ) as client:
        first = client.post(
            "/api/knowledge/ingestions",
            json={
                "collection": "重复提交保护",
                "sources": ["https://example.com/a.pdf", "https://example.com/b.pdf"],
                "pdf_max_pages": 20,
            },
        )
        repeated = client.post(
            "/api/knowledge/ingestions",
            json={
                "collection": "重复提交保护",
                "sources": ["https://example.com/b.pdf", "https://example.com/a.pdf"],
                "pdf_max_pages": 20,
            },
        )
        ingestions = client.get("/api/knowledge/ingestions")

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json()["deduplicated"] is False
    assert repeated.json()["deduplicated"] is True
    assert repeated.json()["id"] == first.json()["id"]
    assert len(ingestions.json()) == 1
    assert repository.job_summary() == {"queued": 1}
