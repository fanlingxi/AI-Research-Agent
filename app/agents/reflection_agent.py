from __future__ import annotations

from app.schemas.quality import CritiqueResult, EvaluationResult, ReflectionResult


class ReflectionAgent:
    """Apply critic feedback by appending a transparent reflection section."""

    def revise(
        self,
        report: str,
        evaluation: EvaluationResult,
        critique: CritiqueResult,
    ) -> ReflectionResult:
        reflection_section = self._build_reflection_section(evaluation, critique)
        revised_report = f"{report.rstrip()}\n\n{reflection_section}"

        applied = []
        if critique.suggestions:
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
