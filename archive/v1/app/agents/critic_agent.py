from __future__ import annotations

import json
import re

from app.config.settings import get_settings
from app.llms.provider import LLMClient, MockLLMClient
from app.schemas.quality import CritiqueResult, EvaluationResult


class CriticAgent:
    """Review the report and turn evaluation metrics into revision guidance."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm

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

        llm_feedback = self._review_with_llm(report=report, evaluation=evaluation)
        issues.extend(llm_feedback.get("issues", []))
        suggestions.extend(llm_feedback.get("suggestions", []))

        critical_names = {
            "retrieval_relevance",
            "graph_quality",
            "citation_faithfulness",
            "source_quality",
        }
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

    def _review_with_llm(
        self,
        report: str,
        evaluation: EvaluationResult,
    ) -> dict[str, list[str]]:
        if self.llm is None or isinstance(self.llm, MockLLMClient):
            return {"issues": [], "suggestions": []}

        metrics = "\n".join(
            f"- {metric.name}: {metric.score:.2f} | {metric.reason}"
            for metric in evaluation.metrics
        )
        prompt = "\n\n".join(
            [
                "请以科研报告审稿人的身份审查以下报告。",
                "只识别与给定证据、引用完整性和推理边界有关的问题；不得编造事实。",
                "返回 JSON：{\"issues\": [\"...\"], \"suggestions\": [\"...\"]}。",
                "评估指标：\n" + metrics,
                "报告内容：\n" + report[:12000],
            ]
        )
        try:
            response = self.llm.invoke(
                prompt,
                system_prompt="你是严格、简洁的中文科研报告 Critic Agent，只返回合法 JSON。",
            )
            payload = self._extract_json(response)
        except Exception:
            return {"issues": [], "suggestions": []}

        issues = payload.get("issues", [])
        suggestions = payload.get("suggestions", [])
        if not isinstance(issues, list) or not isinstance(suggestions, list):
            return {"issues": [], "suggestions": []}
        return {
            "issues": [str(item) for item in issues[:4] if str(item).strip()],
            "suggestions": [str(item) for item in suggestions[:4] if str(item).strip()],
        }

    def _extract_json(self, response: str) -> dict:
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned)
            cleaned = re.sub(r"```$", "", cleaned).strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        return json.loads(match.group(0) if match else cleaned)

    def _suggestion_for_metric(self, metric_name: str) -> str:
        suggestions = {
            "retrieval_coverage": "增加候选论文数量，或提高 retrieval top_k。",
            "retrieval_relevance": "优化检索关键词、增加多查询召回，并剔除偏离主题的候选论文。",
            "graph_quality": "清理图谱噪声实体，并提高图谱路径与研究主题的匹配度。",
            "report_structure": "补充证据、图谱和推理相关的必要报告章节。",
            "citation_faithfulness": "让每条核心结论明确关联到论文、检索切片和来源链接。",
            "source_quality": "使用真实 PDF 或在线论文替换离线占位材料，并补齐来源元数据。",
        }
        return suggestions.get(metric_name, "检查该指标并继续完善报告。")
