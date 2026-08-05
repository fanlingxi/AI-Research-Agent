from __future__ import annotations

import re
import time
from typing import Any, Protocol

from app.config.settings import Settings, get_settings
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.query import KnowledgeQueryService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import ReportEvaluation, ReportEvidence, ResearchReport
from app.llms.provider import LLMClient, MockLLMClient, get_llm_client


class ReportKnowledgeQuery(Protocol):
    def search(
        self, query: str, *, topic_slugs: list[str] | None = None, top_k: int = 8
    ) -> dict[str, Any]: ...


class KnowledgeReportService:
    """Generate reports exclusively from approved Knowledge Core evidence."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        query_service: ReportKnowledgeQuery | None = None,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
        require_live_llm: bool = True,
    ) -> None:
        self.repository = repository
        self.settings = settings or get_settings()
        self.query_service = query_service or KnowledgeQueryService(
            repository, settings=self.settings
        )
        self.llm = llm or get_llm_client(self.settings)
        self.require_live_llm = require_live_llm

    def submit(
        self,
        *,
        query: str,
        topic_slugs: list[str] | None = None,
        top_k: int = 8,
        report_depth: str = "standard",
    ) -> ResearchReport:
        if self.require_live_llm and self._is_mock_llm():
            raise LiveLLMRequiredError("研究报告需要配置真实 LLM，不会回退到 mock。")
        selected_topics = list(dict.fromkeys(topic_slugs or []))
        if not self.repository.published_paper_ids(selected_topics):
            raise ValueError("所选范围内没有已审核、已发布的论文证据。")
        return self.repository.create_report(
            query=query,
            topic_slugs=selected_topics,
            top_k=top_k,
            report_depth=report_depth,
            run_metadata=self._run_metadata(),
        )

    def run(self, report_id: str) -> ResearchReport:
        started = time.perf_counter()
        generation_calls = 0
        input_tokens = 0
        output_tokens = 0
        report = self.repository.update_report(report_id, status="running", error=None)
        try:
            result = self.query_service.search(
                report.query,
                topic_slugs=report.topic_slugs,
                top_k=report.top_k,
            )
            evidence = [ReportEvidence.model_validate(item) for item in result["evidence"]]
            if not evidence:
                raise ValueError("正式知识中没有检索到足以生成报告的正文证据。")
            prompt = self._prompt(report, evidence, result.get("graph", []))
            content = self.llm.invoke(prompt, system_prompt=_REPORT_SYSTEM_PROMPT).strip()
            generation_calls += 1
            usage = self._llm_usage()
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            evaluation = _evaluate(
                content,
                evidence,
                minimum_coverage=self.settings.report_min_citation_coverage,
                revision_applied=False,
            )
            if not evaluation.passed:
                content = self.llm.invoke(
                    self._revision_prompt(content, evidence),
                    system_prompt=_REPORT_SYSTEM_PROMPT,
                ).strip()
                generation_calls += 1
                usage = self._llm_usage()
                input_tokens += usage["input_tokens"]
                output_tokens += usage["output_tokens"]
                evaluation = _evaluate(
                    content,
                    evidence,
                    minimum_coverage=self.settings.report_min_citation_coverage,
                    revision_applied=True,
                )
            run_metadata = self._execution_metadata(
                report,
                generation_calls=generation_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                started=started,
            )
            if not evaluation.passed:
                return self.repository.update_report(
                    report_id,
                    status="failed",
                    content=content,
                    evidence=evidence,
                    evaluation=evaluation,
                    run_metadata=run_metadata,
                    error="报告在一次修订后仍未通过结构、证据覆盖或引用忠实度检查。",
                )
            completed = self.repository.update_report(
                report_id,
                status="completed",
                content=content,
                evidence=evidence,
                evaluation=evaluation,
                run_metadata=run_metadata,
                error=None,
            )
            self.repository.complete_resource_job("report", report_id)
            return completed
        except Exception as exc:
            return self.repository.update_report(
                report_id,
                status="failed",
                run_metadata=self._execution_metadata(
                    report,
                    generation_calls=generation_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    started=started,
                ),
                error=str(exc),
            )

    def _prompt(
        self,
        report: ResearchReport,
        evidence: list[ReportEvidence],
        graph: list[dict[str, Any]],
    ) -> str:
        depth = {
            "brief": "生成简短结论、关键证据和局限。",
            "standard": "生成研究背景、方法比较、主要发现、局限和结论。",
            "deep": "生成深入综述，包含研究脉络、方法比较、证据冲突、局限和研究机会。",
        }[report.report_depth]
        evidence_text = "\n\n".join(
            f"[{item.id}] {item.title} p.{item.page_start}-{item.page_end}\n{item.text}"
            for item in evidence
        )
        graph_text = "\n".join(
            f"- {item.get('source_name')} --{item.get('relation_type')}--> "
            f"{item.get('target_name')}"
            for item in graph
            if item.get("target_name")
        )
        return (
            f"研究问题：{report.query}\n\n写作深度：{depth}\n\n"
            "只允许使用下方证据。每个主要事实后必须使用 [E1] 形式引用，"
            "不得引用未提供的论文或结论。\n\n"
            f"正式正文证据：\n{evidence_text}\n\n正式语义关系：\n{graph_text or '- 暂无'}"
        )

    def _revision_prompt(self, draft: str, evidence: list[ReportEvidence]) -> str:
        valid = ", ".join(f"[{item.id}]" for item in evidence)
        return (
            "修订下面的报告。保留有证据的结论，删除无证据内容。正文必须至少引用一次"
            f"每个合法证据，并且只能使用这些引用：{valid}。\n\n初稿：\n{draft}"
        )

    def _is_mock_llm(self) -> bool:
        return (
            isinstance(self.llm, MockLLMClient)
            or getattr(self.llm, "provider_name", "mock") == "mock"
        )

    def _run_metadata(self) -> dict[str, Any]:
        provider = getattr(self.llm, "provider_name", "unknown")
        model = {
            "openai": self.settings.llm_model,
            "qwen": self.settings.qwen_model,
            "deepseek": self.settings.deepseek_model,
        }.get(provider, "unknown")
        return {
            "prompt_version": REPORT_PROMPT_VERSION,
            "llm_provider": provider,
            "llm_model": model,
            "knowledge_schema_version": self.repository.schema_version(),
            "embedding_provider": self.settings.embedding_provider,
            "embedding_model": self.settings.embedding_model,
            "qdrant_collection": self.settings.knowledge_qdrant_collection,
        }

    def _llm_usage(self) -> dict[str, int]:
        usage = getattr(self.llm, "last_usage", {}) or {}
        return {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        }

    def _estimated_cost(self, input_tokens: int, output_tokens: int) -> float | None:
        input_price = self.settings.llm_input_cost_per_million
        output_price = self.settings.llm_output_cost_per_million
        if input_price is None or output_price is None:
            return None
        cost = input_tokens * input_price / 1_000_000
        cost += output_tokens * output_price / 1_000_000
        return round(cost, 8)

    def _execution_metadata(
        self,
        report: ResearchReport,
        *,
        generation_calls: int,
        input_tokens: int,
        output_tokens: int,
        started: float,
    ) -> dict[str, Any]:
        return {
            **report.run_metadata,
            "generation_calls": generation_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "cost_usd": self._estimated_cost(input_tokens, output_tokens),
        }


def _evaluate(
    content: str,
    evidence: list[ReportEvidence],
    *,
    minimum_coverage: float = 0.9,
    revision_applied: bool,
) -> ReportEvaluation:
    valid_ids = {item.id for item in evidence}
    all_citations = re.findall(r"\[(E\d+)\]", content)
    cited = set(all_citations) & valid_ids
    invalid = sorted(set(all_citations) - valid_ids)
    coverage = len(cited) / len(valid_ids) if valid_ids else 0.0
    grounded = sum(
        bool(item.text.strip()) and item.page_start >= 1 and item.page_end >= item.page_start
        for item in evidence
    )
    grounding = grounded / len(evidence) if evidence else 0.0
    fidelity = (
        sum(citation in valid_ids for citation in all_citations) / len(all_citations)
        if all_citations
        else 0.0
    )
    headings = re.findall(r"^##?\s+\S+", content, flags=re.MULTILINE)
    structure_score = min(1.0, len(headings) / 3)
    return ReportEvaluation(
        evidence_grounding=round(grounding, 4),
        citation_coverage=round(coverage, 4),
        citation_fidelity=round(fidelity, 4),
        structure_score=round(structure_score, 4),
        cited_evidence=sorted(cited),
        invalid_citations=invalid,
        revision_applied=revision_applied,
        passed=(
            grounding == 1.0
            and coverage >= minimum_coverage
            and fidelity == 1.0
            and structure_score >= 0.8
        ),
    )


REPORT_PROMPT_VERSION = "knowledge-report-v1"


_REPORT_SYSTEM_PROMPT = """你是严谨的中文科研报告作者。只能使用用户提供的已审核证据，
每个主要事实必须紧跟 [E编号] 引用。禁止补充外部常识、虚构论文或不存在的实验结果。
输出 Markdown，不要输出代码块。"""
