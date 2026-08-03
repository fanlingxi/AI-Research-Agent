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

        critical_names = {"retrieval_relevance", "graph_quality", "citation_faithfulness"}
        has_critical_issue = any(
            metric.name in critical_names and metric.score < 0.65
            for metric in evaluation.metrics
        )

        return CritiqueResult(
            quality_score=evaluation.overall_score,
            needs_revision=(
                evaluation.overall_score < settings.critic_min_score or has_critical_issue
            ),
            strengths=strengths or ["报告已成功生成。"],
            issues=issues,
            suggestions=list(dict.fromkeys(suggestions)),
        )

    def _suggestion_for_metric(self, metric_name: str) -> str:
        suggestions = {
            "retrieval_coverage": "增加候选论文数量，或提高 retrieval top_k。",
            "retrieval_relevance": "优化检索关键词、增加多查询召回，并剔除偏离主题的候选论文。",
            "graph_quality": "清理图谱噪声实体，并提高图谱路径与研究主题的匹配度。",
            "report_structure": "补充证据、图谱和推理相关的必要报告章节。",
            "citation_faithfulness": "让每条核心结论明确关联到论文、检索切片和来源链接。",
        }
        return suggestions.get(metric_name, "检查该指标并继续完善报告。")
