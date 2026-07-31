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

        has_graphrag_section = "## GraphRAG 推理总结" in report or "## GraphRAG Summary" in report
        if has_graphrag_section:
            strengths.append("报告包含独立的 GraphRAG 推理章节。")
        else:
            issues.append("报告缺少 GraphRAG 推理章节。")
            suggestions.append(
                "补充 GraphRAG 推理总结章节，明确展示图谱路径和向量证据。"
            )

        return CritiqueResult(
            quality_score=evaluation.overall_score,
            needs_revision=evaluation.overall_score < settings.critic_min_score,
            strengths=strengths or ["报告已成功生成。"],
            issues=issues,
            suggestions=list(dict.fromkeys(suggestions)),
        )

    def _suggestion_for_metric(self, metric_name: str) -> str:
        suggestions = {
            "retrieval_coverage": "增加候选论文数量，或提高 retrieval top_k。",
            "graph_quality": "在图谱推理前抽取更多实体和关系。",
            "report_structure": "补充证据、图谱和推理相关的必要报告章节。",
            "grounding": "补充更清晰的来源引用和证据片段。",
        }
        return suggestions.get(metric_name, "检查该指标并继续完善报告。")
