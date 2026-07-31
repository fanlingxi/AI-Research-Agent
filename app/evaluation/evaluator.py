from __future__ import annotations

from app.config.settings import get_settings
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation
from app.schemas.quality import EvaluationMetric, EvaluationResult


class ResearchEvaluator:
    """Evaluate retrieval, graph quality, report structure, and grounding."""

    def evaluate(
        self,
        report: str,
        papers: list[PaperMetadata],
        retrieval_hits: list[RetrievalHit],
        graph_entities: list[GraphEntity],
        graph_relations: list[GraphRelation],
        graph_paths: list[GraphPath],
    ) -> EvaluationResult:
        settings = get_settings()
        metrics = [
            self._retrieval_metric(retrieval_hits),
            self._graph_metric(graph_entities, graph_relations, graph_paths),
            self._report_structure_metric(report),
            self._grounding_metric(report, papers, retrieval_hits, graph_paths),
        ]

        overall = round(sum(metric.score for metric in metrics) / len(metrics), 3)
        return EvaluationResult(
            overall_score=overall,
            passed=overall >= settings.evaluation_passing_score,
            summary=(
                f"综合质量评分为 {overall:.2f}，"
                f"通过阈值为 {settings.evaluation_passing_score:.2f}。"
            ),
            metrics=metrics,
        )

    def _retrieval_metric(self, hits: list[RetrievalHit]) -> EvaluationMetric:
        score = min(1.0, len(hits) / 3)
        return EvaluationMetric(
            name="retrieval_coverage",
            score=round(score, 3),
            reason=f"检索到 {len(hits)} 个向量证据片段。",
        )

    def _graph_metric(
        self,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
        paths: list[GraphPath],
    ) -> EvaluationMetric:
        entity_score = min(1.0, len(entities) / 8)
        relation_score = min(1.0, len(relations) / 12)
        path_score = min(1.0, len(paths) / 3)
        score = (entity_score + relation_score + path_score) / 3
        return EvaluationMetric(
            name="graph_quality",
            score=round(score, 3),
            reason=(
                f"抽取到 {len(entities)} 个实体、{len(relations)} 条关系，"
                f"并检索到 {len(paths)} 条图谱路径。"
            ),
        )

    def _report_structure_metric(self, report: str) -> EvaluationMetric:
        required_sections = [
            "## 候选论文",
            "## 检索证据",
            "## 知识图谱",
            "## GraphRAG 推理总结",
        ]
        present = [section for section in required_sections if section in report]
        score = len(present) / len(required_sections)
        return EvaluationMetric(
            name="report_structure",
            score=round(score, 3),
            reason=f"找到 {len(present)} / {len(required_sections)} 个必要报告章节。",
        )

    def _grounding_metric(
        self,
        report: str,
        papers: list[PaperMetadata],
        hits: list[RetrievalHit],
        paths: list[GraphPath],
    ) -> EvaluationMetric:
        evidence_signals = len(papers) + len(hits) + len(paths)
        has_source_mentions = (
            "来源" in report
            or "Source:" in report
            or "[offline-demo]" in report
            or "arxiv" in report
        )
        score = min(1.0, evidence_signals / 9)
        if has_source_mentions:
            score = min(1.0, score + 0.2)

        return EvaluationMetric(
            name="grounding",
            score=round(score, 3),
            reason=(
                f"检测到 {evidence_signals} 个证据信号；"
                f"是否包含来源信息：{has_source_mentions}。"
            ),
        )
