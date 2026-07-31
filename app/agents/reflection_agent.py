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
            applied.append("Added critic suggestions and evaluation scores to the report.")
        applied.append("Recorded quality review for long-term memory and future runs.")

        return ReflectionResult(
            revised_report=revised_report,
            notes=[
                "Reflection completed after GraphRAG synthesis.",
                "No original evidence was removed during revision.",
            ],
            applied_suggestions=applied,
        )

    def _build_reflection_section(
        self,
        evaluation: EvaluationResult,
        critique: CritiqueResult,
    ) -> str:
        lines = [
            "## Evaluation",
            "",
            f"- Overall score: `{evaluation.overall_score:.2f}`",
            f"- Passed: `{evaluation.passed}`",
            f"- Summary: {evaluation.summary}",
            "",
            "### Metrics",
            "",
        ]
        lines.extend(
            f"- `{metric.name}`: {metric.score:.2f} — {metric.reason}"
            for metric in evaluation.metrics
        )

        lines.extend(["", "## Critic Review", "", "### Strengths", ""])
        lines.extend(f"- {item}" for item in critique.strengths)

        lines.extend(["", "### Issues", ""])
        if critique.issues:
            lines.extend(f"- {item}" for item in critique.issues)
        else:
            lines.append("- No blocking issues found.")

        lines.extend(["", "### Suggestions", ""])
        if critique.suggestions:
            lines.extend(f"- {item}" for item in critique.suggestions)
        else:
            lines.append("- No revision suggestions required.")

        return "\n".join(lines)
