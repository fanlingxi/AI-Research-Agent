from __future__ import annotations

from app.config.settings import get_settings
from app.schemas.quality import CritiqueResult, EvaluationResult


class CriticAgent:
    """Review the report and turn evaluation metrics into revision guidance."""

    def review(
        self,
        report: str,
        evaluation: EvaluationResult,
    ) -> CritiqueResult:
        settings = get_settings()
        strengths = []
        issues = []
        suggestions = []

        for metric in evaluation.metrics:
            if metric.score >= 0.8:
                strengths.append(f"{metric.name}: {metric.reason}")
            else:
                issues.append(f"{metric.name}: {metric.reason}")
                suggestions.append(self._suggestion_for_metric(metric.name))

        if "## GraphRAG Summary" in report:
            strengths.append("The report includes a dedicated GraphRAG reasoning section.")
        else:
            issues.append("The report is missing a GraphRAG reasoning section.")
            suggestions.append(
                "Add a GraphRAG Summary section with graph paths and vector evidence."
            )

        return CritiqueResult(
            quality_score=evaluation.overall_score,
            needs_revision=evaluation.overall_score < settings.critic_min_score,
            strengths=strengths or ["The report was generated successfully."],
            issues=issues,
            suggestions=list(dict.fromkeys(suggestions)),
        )

    def _suggestion_for_metric(self, metric_name: str) -> str:
        suggestions = {
            "retrieval_coverage": "Collect more papers or increase retrieval top_k.",
            "graph_quality": "Extract more entities and relations before graph reasoning.",
            "report_structure": "Add missing report sections for evidence, graph, and reasoning.",
            "grounding": "Add clearer source references and evidence snippets.",
        }
        return suggestions.get(metric_name, "Review this metric and improve the report.")
