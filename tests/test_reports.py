import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.query import KnowledgeQueryService, _latin_query_terms
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan, ReportEvidence
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.llms.provider import MockLLMClient
from app.worker import KnowledgeWorker
from tests.core_fixtures import persist_evidence_chunk


def _repository_with_paper(tmp_path):
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    ingestion = repository.create_ingestion(
        topic="正式报告知识", sources=["paper.pdf"], pdf_max_pages=5, enqueue=False
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="paper:formal",
        chunk_id="paper:formal:page:2:chunk:0",
        page_start=2,
        page_end=2,
        quote="The approved method improves evidence-grounded research synthesis.",
    )
    candidate = CandidateEntity(
        id="candidate-formal-paper",
        ingestion_id=ingestion.id,
        topic_slug=ingestion.topic_slug,
        name="Formal Evidence Paper",
        type="Paper",
        summary="一篇提供正式、可定位证据并用于报告生成的研究论文。",
        confidence=0.95,
        evidence=evidence,
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    repository.add_candidate_entity(candidate)
    repository.publish_entity(candidate.id)
    return repository, ingestion


class _Query:
    def search(self, query, *, topic_slugs=None, top_k=8):
        return {
            "query": query,
            "topic_slugs": topic_slugs or [],
            "graph": [
                {
                    "source_name": "Formal Evidence Paper",
                    "relation_type": "SUPPORTS",
                    "target_name": "Grounded Reports",
                }
            ],
            "evidence": [
                ReportEvidence(
                    id="E1",
                    paper_id="paper:formal",
                    chunk_id="paper:formal:page:2:chunk:0",
                    title="Formal Evidence Paper",
                    text="Approved evidence supports grounded report writing.",
                    page_start=2,
                    page_end=2,
                    score=0.94,
                ).model_dump(),
                ReportEvidence(
                    id="E2",
                    paper_id="paper:formal",
                    chunk_id="paper:formal:page:3:chunk:0",
                    title="Formal Evidence Paper",
                    text="A critic checks citation fidelity before publication.",
                    page_start=3,
                    page_end=3,
                    score=0.9,
                ).model_dump(),
            ][:top_k],
        }


class _RevisingLLM:
    provider_name = "openai"

    def __init__(self):
        self.calls = 0

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls += 1
        if self.calls == 1:
            return "# 初稿\n\n只有一条证据。[E1]"
        return (
            "# 研究报告\n\n## 背景\n正式知识支持证据约束写作。[E1]\n\n"
            "## 评估\n引用忠实度在发布前接受检查。[E2]\n\n"
            "## 结论\n报告结论可回溯到原文页码。[E1][E2]"
        )


class _UngroundedLLM:
    provider_name = "openai"

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        return "# 报告\n\n没有合法引用的结论。"


def _report_stack(tmp_path):
    repository, ingestion = _repository_with_paper(tmp_path)
    settings = Settings(
        knowledge_db_path=repository.path,
        knowledge_vault_path=str(tmp_path / "vault"),
    )
    llm = _RevisingLLM()
    reports = KnowledgeReportService(
        repository,
        query_service=_Query(),
        llm=llm,
        settings=settings,
        require_live_llm=True,
    )
    ingestion_service = KnowledgeIngestionService(
        repository,
        settings=settings,
        indexer=NoopChunkIndexer(),
        projector=NoopKnowledgeProjector(),
        require_live_llm=False,
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=reports,
        lease_seconds=30,
    )
    return repository, ingestion, reports, worker, llm


def test_report_worker_revises_once_and_persists_evidence_evaluation(tmp_path) -> None:
    repository, ingestion, reports, worker, llm = _report_stack(tmp_path)
    submitted = reports.submit(
        query="如何生成有据可查的研究报告？",
        topic_slugs=[ingestion.topic_slug],
        top_k=2,
        report_depth="standard",
    )

    assert submitted.status == "queued"
    assert worker.run_once()
    completed = repository.get_report(submitted.id)

    assert completed.status == "completed"
    assert llm.calls == 2
    assert completed.evaluation is not None
    assert completed.evaluation.passed
    assert completed.evaluation.revision_applied
    assert completed.evaluation.evidence_grounding == 1.0
    assert completed.evaluation.citation_coverage == 1.0
    assert completed.evaluation.citation_fidelity == 1.0
    assert len(completed.evidence) == 2
    assert completed.run_metadata["prompt_version"] == "knowledge-report-v1"
    assert completed.run_metadata["generation_calls"] == 2
    assert completed.run_metadata["latency_ms"] >= 0
    assert repository.job_summary()["completed"] == 1


def test_reports_never_fall_back_to_mock_or_unpublished_evidence(tmp_path) -> None:
    empty = KnowledgeRepository(str(tmp_path / "empty.db"))
    with pytest.raises(LiveLLMRequiredError):
        KnowledgeReportService(empty, llm=MockLLMClient()).submit(query="test")

    service = KnowledgeReportService(
        empty,
        query_service=_Query(),
        llm=_RevisingLLM(),
        require_live_llm=True,
    )
    with pytest.raises(ValueError, match="没有已审核"):
        service.submit(query="test")


def test_report_fails_after_the_single_revision_budget_is_exhausted(tmp_path) -> None:
    repository, _ = _repository_with_paper(tmp_path)
    service = KnowledgeReportService(
        repository,
        query_service=_Query(),
        llm=_UngroundedLLM(),
        require_live_llm=True,
    )
    submitted = service.submit(query="无法通过质量门的报告", top_k=2)

    failed = service.run(submitted.id)

    assert failed.status == "failed"
    assert failed.evaluation is not None
    assert failed.evaluation.revision_applied
    assert not failed.evaluation.passed
    assert "一次修订" in (failed.error or "")


class _ChunkSearch:
    def __init__(self):
        self.allowed = set()

    def search(self, query, *, allowed_paper_ids, top_k):
        self.allowed = allowed_paper_ids
        return []


class _GraphSearch:
    def search(self, query, *, topic_slugs, limit=20):
        return []


class _EvidenceChunkSearch:
    def search(self, query, *, allowed_paper_ids, top_k):
        return [
            ReportEvidence(
                id="E-reference",
                paper_id="paper:formal",
                chunk_id="reference",
                title="Formal Evidence Paper",
                text="References [1] Agent systems and related work.",
                page_start=20,
                page_end=20,
                score=0.99,
            ),
            ReportEvidence(
                id="E-main",
                paper_id="paper:formal",
                chunk_id="main",
                title="Formal Evidence Paper",
                text="An agent uses verified evidence to plan a research task.",
                page_start=2,
                page_end=2,
                score=0.32,
            ),
            ReportEvidence(
                id="E-second",
                paper_id="paper:formal",
                chunk_id="second",
                title="Formal Evidence Paper",
                text="The agent checks evidence coverage before publication.",
                page_start=3,
                page_end=3,
                score=0.28,
            ),
            ReportEvidence(
                id="E-third",
                paper_id="paper:formal",
                chunk_id="third",
                title="Formal Evidence Paper",
                text="Agent runs retain a reviewable execution trace.",
                page_start=4,
                page_end=4,
                score=0.27,
            ),
            ReportEvidence(
                id="E-other",
                paper_id="paper:other",
                chunk_id="other",
                title="Other Paper",
                text="Another agent evaluates source-grounded answers.",
                page_start=1,
                page_end=1,
                score=0.3,
            ),
        ]


class _UnavailableGraphSearch:
    def search(self, query, *, topic_slugs, limit=20):
        raise RuntimeError("neo4j is unavailable")


def test_query_service_filters_chunks_by_sqlite_published_paper_ids(tmp_path) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    chunks = _ChunkSearch()
    query = KnowledgeQueryService(
        repository,
        chunk_search=chunks,
        graph_search=_GraphSearch(),
    )

    query.search("grounded", topic_slugs=[ingestion.topic_slug], top_k=5)

    assert chunks.allowed == {"paper:formal"}


def test_query_service_filters_bibliography_diversifies_results_and_degrades_graph(
    tmp_path,
) -> None:
    repository, ingestion = _repository_with_paper(tmp_path)
    query = KnowledgeQueryService(
        repository,
        chunk_search=_EvidenceChunkSearch(),
        graph_search=_UnavailableGraphSearch(),
    )

    result = query.search("agent", topic_slugs=[ingestion.topic_slug], top_k=3)

    assert len(result["evidence"]) == 3
    assert all(item["chunk_id"] != "reference" for item in result["evidence"])
    assert sum(item["paper_id"] == "paper:formal" for item in result["evidence"]) == 2
    assert any(item["paper_id"] == "paper:other" for item in result["evidence"])
    assert result["graph"] == []
    assert result["warnings"] == ["图关系服务暂时不可用，当前仅展示已定位的原文证据。"]


def test_hash_query_keeps_bilingual_technical_anchors() -> None:
    assert _latin_query_terms("Toolformer 如何决定调用哪个 API？") == "toolformer api"
    assert _latin_query_terms("纯中文问题") == ""


def test_report_api_exposes_content_evidence_and_download(tmp_path) -> None:
    repository, ingestion, reports, worker, _ = _report_stack(tmp_path)
    with TestClient(
        create_app(
            knowledge_repository=repository,
            knowledge_service=worker.ingestion_service,
            report_service=reports,
        )
    ) as client:
        submitted = client.post(
            "/api/reports",
            json={
                "query": "如何生成有据可查的研究报告？",
                "topic_slugs": [ingestion.topic_slug],
                "top_k": 2,
                "report_depth": "standard",
            },
        )
        report_id = submitted.json()["id"]
        assert worker.run_once()
        detail = client.get(f"/api/reports/{report_id}")
        evidence = client.get(f"/api/reports/{report_id}/evidence")
        download = client.get(f"/api/reports/{report_id}/download")

    assert submitted.status_code == 202
    assert detail.json()["status"] == "completed"
    assert len(evidence.json()["evidence"]) == 2
    assert download.status_code == 200
    assert "研究报告" in download.text
    assert download.headers["content-type"].startswith("text/markdown")
