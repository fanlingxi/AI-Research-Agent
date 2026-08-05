"""Isolated local server used only by the browser E2E smoke test."""

from __future__ import annotations

import os
import threading
import time

from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import (
    EvidenceSpan,
    ExtractedEntity,
    KnowledgeExtraction,
    PaperReading,
    ReportEvidence,
)
from app.knowledge.service import (
    KnowledgeIngestionService,
    NoopChunkIndexer,
    NoopKnowledgeProjector,
)
from app.schemas.documents import ParsedDocument
from app.worker import KnowledgeWorker


class _Extractor:
    def extract(self, paper, chunks):
        evidence = EvidenceSpan(
            paper_id=paper.id,
            chunk_id=chunks[0].id,
            page_start=1,
            page_end=1,
            quote="Browser E2E evidence supports a reviewable knowledge report.",
        )
        return ExtractionResult(
            KnowledgeExtraction(
                reading=PaperReading(
                    research_problem="验证浏览器端知识入库、审核与报告链路能够完整运行。",
                    core_contributions=["提供隔离且可重复的浏览器端到端验证。"],
                    method_summary="使用临时事实库、fixture PDF 和本地确定性模型完成验证。",
                    evidence=evidence,
                ),
                entities=[
                    ExtractedEntity(
                        name="浏览器端到端验证",
                        type="Method",
                        summary="在隔离环境中覆盖入库、审核、检索和报告生成的验证方法。",
                        aliases=["Browser E2E"],
                        confidence=0.95,
                        evidence=evidence,
                    )
                ],
            )
        )


def _parse(*, source: str, max_pages: int) -> ParsedDocument:
    return ParsedDocument(
        source=source,
        title="Browser E2E Paper",
        text="Browser E2E evidence supports a reviewable knowledge report.",
        pages=1,
        page_texts=["Browser E2E evidence supports a reviewable knowledge report."],
        page_numbers=[1],
    )


class _Query:
    def __init__(self, repository: KnowledgeRepository) -> None:
        self.repository = repository

    def search(self, query, *, topic_slugs=None, top_k=8):
        paper_id = next(iter(self.repository.published_paper_ids(topic_slugs or [])))
        return {
            "query": query,
            "topic_slugs": topic_slugs or [],
            "graph": [],
            "evidence": [
                ReportEvidence(
                    id="E1",
                    paper_id=paper_id,
                    chunk_id=f"{paper_id}:page:1:chunk:0",
                    title="Browser E2E Paper",
                    text="Approved evidence supports the full browser workflow.",
                    page_start=1,
                    page_end=1,
                    score=1.0,
                ).model_dump()
            ],
        }


class _LLM:
    provider_name = "fixture"
    last_usage = {"input_tokens": 20, "output_tokens": 30}

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        return (
            "# 浏览器 E2E 报告\n\n## 背景\n正式证据驱动整个流程。[E1]\n\n"
            "## 结果\n入库、审核和报告链路已经连通。[E1]\n\n"
            "## 结论\n主要结论可回溯到 PDF 页码。[E1]"
        )


database_path = os.environ["E2E_DB_PATH"]
vault_path = os.environ["E2E_VAULT_PATH"]
settings = Settings(
    knowledge_db_path=database_path,
    knowledge_vault_path=vault_path,
    chunk_size=40,
    chunk_overlap=10,
)
repository = KnowledgeRepository(database_path)
ingestion_service = KnowledgeIngestionService(
    repository,
    settings=settings,
    extractor=_Extractor(),
    parser=_parse,
    indexer=NoopChunkIndexer(),
    projector=NoopKnowledgeProjector(),
    vault_exporter=KnowledgeVaultExporter(vault_path),
    require_live_llm=False,
)
report_service = KnowledgeReportService(
    repository,
    query_service=_Query(repository),
    llm=_LLM(),
    settings=settings,
    require_live_llm=False,
)
worker = KnowledgeWorker(
    repository,
    ingestion_service=ingestion_service,
    report_service=report_service,
    lease_seconds=30,
)
app = create_app(
    knowledge_repository=repository,
    knowledge_service=ingestion_service,
    report_service=report_service,
)


def _work() -> None:
    while True:
        if not worker.run_once():
            time.sleep(0.05)


threading.Thread(target=_work, daemon=True, name="knowledge-e2e-worker").start()
