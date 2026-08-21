from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Literal, Protocol

from app.config.settings import Settings, get_settings
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.query import KnowledgeQueryService
from app.knowledge.repository import KnowledgeRepository, StaleReportExecution
from app.knowledge.schemas import KnowledgeJob, ReportEvaluation, ReportEvidence, ResearchReport
from app.llms.provider import LLMClient, MockLLMClient, get_llm_client

logger = logging.getLogger(__name__)


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
        auto_execute: bool = False,
    ) -> ResearchReport:
        if self.require_live_llm and self._is_mock_llm():
            raise LiveLLMRequiredError("研究报告需要配置真实 LLM，不会回退到 mock。")
        selected_topics = list(dict.fromkeys(topic_slugs or []))
        if not self.repository.published_paper_ids(selected_topics):
            raise ValueError("所选范围内没有已审核、已发布的论文证据。")
        run_metadata = self._run_metadata()
        if auto_execute:
            run_metadata["current_stage"] = "queued_for_dispatch"
        return self.repository.create_report(
            query=query,
            topic_slugs=selected_topics,
            top_k=top_k,
            report_depth=report_depth,
            run_metadata=run_metadata,
            auto_execute=auto_execute,
        )

    def run(
        self,
        report_id: str,
        *,
        expected_job_id: str | None = None,
        expected_job_attempt: int | None = None,
        expected_job_owner: str | None = None,
    ) -> ResearchReport:
        if (expected_job_id is None) != (expected_job_attempt is None):
            raise ValueError("A claimed report requires both its job id and attempt.")
        started = time.perf_counter()
        generation_calls = 0
        input_tokens = 0
        output_tokens = 0
        report = self.repository.get_report(report_id)
        report = self.repository.update_report(
            report_id,
            status="running",
            run_metadata={**report.run_metadata, "current_stage": "retrieving_evidence"},
            error=None,
            expected_job_attempt=expected_job_attempt,
            expected_job_owner=expected_job_owner,
        )
        try:
            result = self.query_service.search(
                report.query,
                topic_slugs=report.topic_slugs,
                top_k=report.top_k,
            )
            evidence = [ReportEvidence.model_validate(item) for item in result["evidence"]]
            if not evidence:
                raise ValueError("正式知识中没有检索到足以生成报告的正文证据。")
            self.repository.update_report(
                report_id,
                status="running",
                evidence=evidence,
                run_metadata={**report.run_metadata, "current_stage": "generating_draft"},
                error=None,
                expected_job_attempt=expected_job_attempt,
                expected_job_owner=expected_job_owner,
            )
            prompt = self._prompt(report, evidence, result.get("graph", []))
            content = self.llm.invoke(prompt, system_prompt=_REPORT_SYSTEM_PROMPT).strip()
            generation_calls += 1
            usage = self._llm_usage()
            input_tokens += usage["input_tokens"]
            output_tokens += usage["output_tokens"]
            self.repository.update_report(
                report_id,
                status="running",
                evidence=evidence,
                run_metadata={**report.run_metadata, "current_stage": "validating_citations"},
                error=None,
                expected_job_attempt=expected_job_attempt,
                expected_job_owner=expected_job_owner,
            )
            evaluation = _evaluate(
                content,
                evidence,
                minimum_coverage=self.settings.report_min_citation_coverage,
                revision_applied=False,
            )
            if not evaluation.passed:
                self.repository.update_report(
                    report_id,
                    status="running",
                    evidence=evidence,
                    run_metadata={**report.run_metadata, "current_stage": "revising_draft"},
                    error=None,
                    expected_job_attempt=expected_job_attempt,
                    expected_job_owner=expected_job_owner,
                )
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
                return self._finalize_run(
                    report_id,
                    expected_job_id=expected_job_id,
                    expected_job_attempt=expected_job_attempt,
                    expected_job_owner=expected_job_owner,
                    status="failed",
                    content=content,
                    evidence=evidence,
                    evaluation=evaluation,
                    run_metadata={**run_metadata, "current_stage": "failed"},
                    error="报告在一次修订后仍未通过结构、证据覆盖或引用忠实度检查。",
                )
            completed = self._finalize_run(
                report_id,
                expected_job_id=expected_job_id,
                expected_job_attempt=expected_job_attempt,
                expected_job_owner=expected_job_owner,
                status="completed",
                content=content,
                evidence=evidence,
                evaluation=evaluation,
                run_metadata={**run_metadata, "current_stage": "completed"},
                error=None,
            )
            return completed
        except StaleReportExecution:
            raise
        except Exception as exc:
            return self._finalize_run(
                report_id,
                expected_job_id=expected_job_id,
                expected_job_attempt=expected_job_attempt,
                expected_job_owner=expected_job_owner,
                status="failed",
                run_metadata=self._execution_metadata(
                    report,
                    generation_calls=generation_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    started=started,
                )
                | {"current_stage": "failed"},
                error=str(exc),
            )

    def execute_claimed(
        self,
        report_id: str,
        job_id: str,
        expected_attempt: int,
        expected_owner: str | None = None,
    ) -> ResearchReport:
        """Execute an already claimed report and keep report/job terminal states aligned."""
        try:
            return self.run(
                report_id,
                expected_job_id=job_id,
                expected_job_attempt=expected_attempt,
                expected_job_owner=expected_owner,
            )
        except StaleReportExecution:
            return self.repository.get_report(report_id)
        except Exception as exc:
            try:
                return self.fail_claimed(
                    report_id,
                    job_id,
                    expected_attempt,
                    str(exc),
                    expected_owner=expected_owner,
                )
            except StaleReportExecution:
                return self.repository.get_report(report_id)

    def execute_claimed_with_heartbeat(
        self,
        job: KnowledgeJob,
        *,
        lease_seconds: int,
    ) -> ResearchReport:
        """Run one claimed report while renewing the same fenced attempt."""
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_claimed_lease,
            args=(job, lease_seconds, heartbeat_stop),
            daemon=True,
            name=f"report-lease-{job.id}",
        )
        heartbeat.start()
        try:
            return self.execute_claimed(
                job.resource_id,
                job.id,
                job.attempts,
                expected_owner=job.lease_owner,
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)

    def _renew_claimed_lease(
        self,
        job: KnowledgeJob,
        lease_seconds: int,
        stopped: threading.Event,
    ) -> None:
        interval = max(0.05, min(30.0, max(1, lease_seconds) / 3))
        while not stopped.wait(interval):
            try:
                renewed = self.repository.renew_job_lease(
                    job.id,
                    expected_attempt=job.attempts,
                    lease_seconds=lease_seconds,
                    expected_owner=job.lease_owner,
                )
            except Exception:
                logger.exception("Failed to renew report lease for %s", job.resource_id)
                continue
            if not renewed:
                return

    def fail_claimed(
        self,
        report_id: str,
        job_id: str,
        expected_attempt: int,
        error: str,
        *,
        expected_owner: str | None = None,
    ) -> ResearchReport:
        """Atomically fail a claimed execution if this attempt still owns it."""
        report = self.repository.get_report(report_id)
        return self.repository.finalize_report_execution(
            report_id,
            job_id,
            expected_attempt=expected_attempt,
            status="failed",
            run_metadata={**report.run_metadata, "current_stage": "failed"},
            error=error,
            expected_owner=expected_owner,
        )

    def _finalize_run(
        self,
        report_id: str,
        *,
        expected_job_id: str | None,
        expected_job_attempt: int | None,
        expected_job_owner: str | None,
        status: Literal["completed", "failed"],
        content: str | None = None,
        evidence: list[ReportEvidence] | None = None,
        evaluation: ReportEvaluation | None = None,
        run_metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ResearchReport:
        if expected_job_id is not None and expected_job_attempt is not None:
            return self.repository.finalize_report_execution(
                report_id,
                expected_job_id,
                expected_attempt=expected_job_attempt,
                status=status,
                content=content,
                evidence=evidence,
                evaluation=evaluation,
                run_metadata=run_metadata,
                error=error,
                expected_owner=expected_job_owner,
            )
        result = self.repository.update_report(
            report_id,
            status=status,
            content=content,
            evidence=evidence,
            evaluation=evaluation,
            run_metadata=run_metadata,
            error=error,
        )
        job = self.repository.get_resource_job("report", report_id)
        if status == "failed":
            self.repository.fail_job(job.id, error or "报告生成失败")
        else:
            self.repository.complete_job(job.id)
        return result

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


class KnowledgeReportDispatcher:
    """In-process durable dispatcher for reports explicitly started from the API."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        service: KnowledgeReportService,
        *,
        lease_seconds: int = 180,
        poll_seconds: float = 0.5,
    ) -> None:
        self.repository = repository
        self.service = service
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="knowledge-report-dispatcher",
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def wake(self) -> None:
        self._wake.set()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.repository.claim_dispatched_report_job(self.lease_seconds)
            except Exception:
                logger.exception("Failed to claim a dispatched report job")
                job = None
            if job is not None:
                self._execute(job)
                continue
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _execute(self, job: KnowledgeJob) -> None:
        try:
            self.service.execute_claimed_with_heartbeat(
                job,
                lease_seconds=self.lease_seconds,
            )
        except Exception as exc:
            logger.exception("Report dispatcher failed while executing %s", job.resource_id)
            try:
                self.service.fail_claimed(job.resource_id, job.id, job.attempts, str(exc))
            except StaleReportExecution:
                pass
            except Exception:
                logger.exception("Failed to persist report dispatcher failure")


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
