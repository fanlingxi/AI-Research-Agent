"""Isolated local server used only by the browser E2E smoke test."""

# ruff: noqa: E402 -- test environment must be configured before app imports.

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# This module is started by uvicorn rather than pytest, so pytest's conftest
# isolation is not available. Set every standard application setting before
# importing application services, keeping this fixture independent from both
# inherited .env values and future startup implementation details.
database_path = os.environ["E2E_DB_PATH"]
vault_path = os.environ["E2E_VAULT_PATH"]
checkpoint_path = os.environ["E2E_CHECKPOINT_PATH"]
os.environ.update(
    {
        "LLM_PROVIDER": "mock",
        "KNOWLEDGE_DB_PATH": database_path,
        "KNOWLEDGE_VAULT_PATH": vault_path,
        "AGENT_CHECKPOINT_PATH": checkpoint_path,
        "XDG_CACHE_HOME": str(Path(database_path).parent / "cache"),
        "QDRANT_URL": "http://127.0.0.1:9",
        "NEO4J_URI": "bolt://127.0.0.1:9",
    }
)

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.api.main import create_app
from app.config.settings import Settings
from app.knowledge.extractor import ExtractionResult
from app.knowledge.obsidian import KnowledgeVaultExporter
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import (
    CandidateEntity,
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
from tests.core_fixtures import persist_evidence_chunk


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


class _BrowserAcceptanceLLM:
    """Test-only scripted research model for the isolated browser fixture."""

    provider_name = "browser_fixture"
    last_usage = {"input_tokens": 24, "output_tokens": 36}

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        if "Return {research_question, intended_output, constraints}." in prompt:
            return json.dumps(
                {
                    "research_question": "What does the scoped fixture evidence establish?",
                    "intended_output": "A concise cited browser acceptance report.",
                    "constraints": ["Use only the immutable ContextSnapshot."],
                },
                ensure_ascii=False,
            )
        if "Return {steps, tool_sequence}." in prompt:
            return json.dumps(
                {
                    "steps": ["Read the scoped research input.", "Write cited findings."],
                    "tool_sequence": ["context.research_input"],
                },
                ensure_ascii=False,
            )
        if "Return {title, executive_summary, findings, limitations, markdown," in prompt:
            raw_input = prompt.split("Research input: ", 1)[1].rsplit("\nReturn", 1)[0]
            payload = json.loads(raw_input)
            bundle = payload["knowledge"]["claim_bundles"][0]
            claim_id = bundle["claim"]["claim_id"]
            evidence_id = bundle["evidence"][0]["evidence_id"]
            return json.dumps(
                {
                    "title": "Browser Acceptance Research Report",
                    "executive_summary": (
                        "The scoped fixture proves the browser workflow can retain cited evidence."
                    ),
                    "findings": [
                        {
                            "claim_bundle_id": claim_id,
                            "assertion": "The selected claim has a locatable evidence chain.",
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "limitations": ["This report is generated from an isolated browser fixture."],
                    "markdown": (
                        "# Browser Acceptance Research Report\n\n"
                        "The selected claim has a locatable evidence chain. "
                        f"[cite:{evidence_id}]"
                    ),
                    "memory_proposal": {
                        "summary": "Keep ContextSnapshot scope explicit.",
                        "rationale": "The cited report depends on the immutable scoped input.",
                        "impact": (
                            "Future research tasks should preserve the same evidence boundary."
                        ),
                    },
                },
                ensure_ascii=False,
            )
        raise AssertionError("Unexpected browser fixture research prompt")


settings = Settings(
    llm_provider="mock",
    knowledge_db_path=database_path,
    knowledge_vault_path=vault_path,
    agent_checkpoint_path=checkpoint_path,
    chunk_size=40,
    chunk_overlap=10,
)
repository = KnowledgeRepository(database_path)


def _seed_browser_acceptance_knowledge() -> None:
    collection = repository.create_collection("Browser Acceptance Scope")
    ingestion = repository.create_ingestion(
        collection=collection.slug,
        sources=["browser-acceptance-fixture.pdf"],
        pdf_max_pages=1,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="browser-acceptance-paper",
        chunk_id="browser-acceptance-paper:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="A scoped ContextSnapshot keeps a formal claim linked to locatable evidence.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    repository.add_candidate_entity(
        CandidateEntity(
            id="browser-acceptance-published-candidate",
            ingestion_id=ingestion.id,
            topic_slug=collection.slug,
            name="Scoped ContextSnapshot",
            type="Method",
            summary="An immutable, evidence-bounded context package used for browser acceptance.",
            confidence=0.98,
            evidence=evidence,
        )
    )
    repository.publish_entity("browser-acceptance-published-candidate")
    repository.add_candidate_entity(
        CandidateEntity(
            id="browser-acceptance-draft-candidate",
            ingestion_id=ingestion.id,
            topic_slug=collection.slug,
            name="Fixture Candidate Review",
            type="Finding",
            summary="A draft candidate retained only to exercise the browser review flow.",
            confidence=0.82,
            evidence=evidence,
        )
    )


_seed_browser_acceptance_knowledge()
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
agent_service = AgentRunService(
    repository,
    settings=settings,
    llm=_BrowserAcceptanceLLM(),
)
agent_runtime = AgentRuntime(
    agent_service,
    checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
)
worker = KnowledgeWorker(
    repository,
    ingestion_service=ingestion_service,
    report_service=report_service,
    agent_runtime=agent_runtime,
    lease_seconds=30,
)
app = create_app(
    knowledge_repository=repository,
    knowledge_service=ingestion_service,
    report_service=report_service,
    agent_run_service=agent_service,
)


def _work() -> None:
    while True:
        if not worker.run_once():
            time.sleep(0.05)


if os.environ.get("E2E_AUTOSTART_WORKER", "1") == "1":
    threading.Thread(target=_work, daemon=True, name="knowledge-e2e-worker").start()
