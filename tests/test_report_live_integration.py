from __future__ import annotations

import os

import pytest

from app.config.settings import Settings
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateEntity, EvidenceSpan, ReportEvidence
from tests.core_fixtures import persist_evidence_chunk

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_LLM_INTEGRATION") != "1",
    reason="set RUN_LIVE_LLM_INTEGRATION=1 with a configured live LLM",
)


class _SyntheticQuery:
    def search(self, query, *, topic_slugs=None, top_k=8):
        return {
            "query": query,
            "topic_slugs": topic_slugs or [],
            "graph": [
                {
                    "source_name": "Synthetic Reliability Paper",
                    "relation_type": "SUPPORTS",
                    "target_name": "Durable Review",
                }
            ],
            "evidence": [
                ReportEvidence(
                    id="E1",
                    paper_id="paper:synthetic-public",
                    chunk_id="paper:synthetic-public:page:1:chunk:0",
                    title="Synthetic Reliability Paper",
                    text=(
                        "This synthetic test states that an atomic review transaction stores "
                        "the formal fact and its projection intent together."
                    ),
                    page_start=1,
                    page_end=1,
                    score=1.0,
                ).model_dump(),
                ReportEvidence(
                    id="E2",
                    paper_id="paper:synthetic-public",
                    chunk_id="paper:synthetic-public:page:2:chunk:0",
                    title="Synthetic Reliability Paper",
                    text=(
                        "This synthetic test states that a worker can replay an idempotent "
                        "projection after a downstream service recovers."
                    ),
                    page_start=2,
                    page_end=2,
                    score=0.99,
                ).model_dump(),
            ][:top_k],
        }


def test_live_report_is_grounded_and_versioned_without_local_user_data(tmp_path) -> None:
    configured = Settings()
    assert configured.llm_provider != "mock"
    settings = configured.model_copy(update={"knowledge_db_path": str(tmp_path / "knowledge.db")})
    repository = KnowledgeRepository(settings.knowledge_db_path)
    ingestion = repository.create_ingestion(
        topic="Synthetic Public Reliability Test",
        sources=["synthetic-public.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="paper:synthetic-public",
        chunk_id="paper:synthetic-public:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="Atomic review stores the fact and projection intent together.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    repository.add_candidate_entity(
        CandidateEntity(
            id="candidate-synthetic-public-paper",
            ingestion_id=ingestion.id,
            topic_slug=ingestion.topic_slug,
            name="Synthetic Reliability Paper",
            type="Paper",
            summary="完全合成且不含用户数据的可靠审核与投影测试论文。",
            confidence=1.0,
            evidence=evidence,
        )
    )
    repository.publish_entity("candidate-synthetic-public-paper")
    service = KnowledgeReportService(
        repository,
        settings=settings,
        query_service=_SyntheticQuery(),
        require_live_llm=True,
    )

    report = service.submit(
        query="原子审核和幂等投影如何提高本地知识系统的可靠性？",
        top_k=2,
        report_depth="brief",
    )
    completed = service.run(report.id)

    assert completed.status == "completed", completed.error
    assert completed.evaluation is not None
    assert completed.evaluation.evidence_grounding == 1.0
    assert completed.evaluation.citation_coverage >= 0.9
    assert completed.evaluation.citation_fidelity == 1.0
    assert completed.run_metadata["prompt_version"] == "knowledge-report-v3"
    assert completed.run_metadata["llm_provider"] == configured.llm_provider
    assert completed.run_metadata["generation_calls"] in {1, 2}
    assert completed.run_metadata["input_tokens"] > 0
    assert completed.run_metadata["output_tokens"] > 0
    assert completed.run_metadata["latency_ms"] > 0
