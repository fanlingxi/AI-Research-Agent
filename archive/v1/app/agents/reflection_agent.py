from __future__ import annotations

from app.agents.writer_agent import WriterAgent
from app.schemas.quality import CritiqueResult, EvaluationResult, ReflectionResult


class ReflectionAgent:
    """Apply critic feedback through a bounded Writer revision and quality audit."""

    def __init__(self, writer: WriterAgent | None = None) -> None:
        self.writer = writer or WriterAgent()

    def revise(
        self,
        report: str,
        evaluation: EvaluationResult,
        critique: CritiqueResult,
        query: str = "",
        include_audit: bool = True,
    ) -> ReflectionResult:
        revised_report = self.writer.revise(report=report, critique=critique, query=query)
        if include_audit:
            revised_report = self.append_quality_audit(
                report=revised_report,
                evaluation=evaluation,
                critique=critique,
            )

        applied = []
        if critique.needs_revision:
            applied.append("已基于 Critic 建议调用 Writer Agent 进行一次受控修订。")
        if include_audit:
            applied.append("已将 Critic 建议和评估分数写入报告。")
        applied.append("已记录质量审查结果，供长期记忆和后续运行复用。")

        return ReflectionResult(
            revised_report=revised_report,
            notes=[
                "已在 GraphRAG 综合推理后完成反思修订。",
                "修订过程中没有移除原始证据。",
            ],
            applied_suggestions=applied,
        )

    def append_quality_audit(
        self,
        report: str,
        evaluation: EvaluationResult,
        critique: CritiqueResult,
    ) -> str:
        reflection_section = self._build_reflection_section(evaluation, critique)
        return f"{report.rstrip()}\n\n{reflection_section}"

    def _build_reflection_section(
        self,
        evaluation: EvaluationResult,
        critique: CritiqueResult,
    ) -> str:
        lines = [
            "## 评估结果",
            "",
            f"- 综合评分：`{evaluation.overall_score:.2f}`",
            f"- 是否通过：`{evaluation.passed}`",
            f"- 证据状态：`{evaluation.evidence_status}`",
            f"- 是否允许沉淀：`{evaluation.evidence_admissible}`",
            f"- 总结：{evaluation.summary}",
            "",
            "### 指标明细",
            "",
        ]
        lines.extend(
            f"- `{metric.name}`: {metric.score:.2f} — {metric.reason}"
            for metric in evaluation.metrics
        )

        lines.extend(["", "## Critic 审查", "", "### 优点", ""])
        lines.extend(f"- {item}" for item in critique.strengths)

        lines.extend(["", "### 问题", ""])
        if critique.issues:
            lines.extend(f"- {item}" for item in critique.issues)
        else:
            lines.append("- 未发现阻塞性问题。")

        lines.extend(["", "### 修改建议", ""])
        if critique.suggestions:
            lines.extend(f"- {item}" for item in critique.suggestions)
        else:
            lines.append("- 暂无必须修改的建议。")

        return "\n".join(lines)
