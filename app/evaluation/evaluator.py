from __future__ import annotations

from app.config.settings import get_settings
from app.retrieval.relevance import (
    TOPIC_CONCEPTS,
    contains_concept,
    extract_topic_concepts,
    relevance_score,
)
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
        query: str = "",
    ) -> EvaluationResult:
        settings = get_settings()
        metrics = [
            self._retrieval_metric(retrieval_hits),
            self._retrieval_relevance_metric(query, retrieval_hits),
            self._graph_metric(query, graph_entities, graph_relations, graph_paths),
            self._report_structure_metric(report),
            self._citation_metric(report, papers, retrieval_hits),
        ]

        overall = round(sum(metric.score for metric in metrics) / len(metrics), 3)
        critical_names = {"retrieval_relevance", "graph_quality", "citation_faithfulness"}
        critical_failures = [
            metric.name
            for metric in metrics
            if metric.name in critical_names
            and metric.score < settings.evaluation_critical_min_score
        ]
        passed = overall >= settings.evaluation_passing_score and not critical_failures
        gate_note = (
            "关键指标均通过门槛。"
            if not critical_failures
            else "关键指标未通过：" + "、".join(critical_failures) + "。"
        )
        return EvaluationResult(
            overall_score=overall,
            passed=passed,
            summary=(
                f"综合质量评分为 {overall:.2f}，"
                f"通过阈值为 {settings.evaluation_passing_score:.2f}。{gate_note}"
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
        query: str,
        entities: list[GraphEntity],
        relations: list[GraphRelation],
        paths: list[GraphPath],
    ) -> EvaluationMetric:
        noise_names = {"this", "these", "arxiv", "evaluation"}
        useful_entities = [
            entity for entity in entities if entity.name.strip().lower() not in noise_names
        ]
        entity_score = min(1.0, len(useful_entities) / 8)
        relation_score = min(1.0, len(relations) / 12)
        if paths:
            path_relevance = sum(
                relevance_score(query, " ".join(node.name for node in path.nodes))
                for path in paths
            ) / len(paths)
            path_score = min(1.0, len(paths) / 3) * path_relevance
        else:
            path_score = 0.0
        score = 0.25 * entity_score + 0.25 * relation_score + 0.5 * path_score
        return EvaluationMetric(
            name="graph_quality",
            score=round(score, 3),
            reason=(
                f"抽取到 {len(useful_entities)} 个有效实体、{len(relations)} 条关系，"
                f"并检索到 {len(paths)} 条图谱路径；路径主题相关度为 {path_score:.2f}。"
            ),
        )

    def _retrieval_relevance_metric(
        self,
        query: str,
        hits: list[RetrievalHit],
    ) -> EvaluationMetric:
        if not hits:
            return EvaluationMetric(
                name="retrieval_relevance",
                score=0.0,
                reason="没有可用于主题相关性评估的检索证据。",
            )

        scores = [relevance_score(query, f"{hit.title}\n{hit.text}") for hit in hits]
        top_hits = sorted(scores, reverse=True)[: min(3, len(scores))]
        top_score = sum(top_hits) / len(top_hits)
        concepts = extract_topic_concepts(query)
        if concepts:
            corpus_text = "\n".join(f"{hit.title}\n{hit.text}" for hit in hits)
            corpus_coverage = sum(
                contains_concept(corpus_text, TOPIC_CONCEPTS[concept])
                for concept in concepts
            ) / len(concepts)
            score = 0.55 * top_score + 0.45 * corpus_coverage
            reason = (
                f"高相关证据平均相关度为 {top_score:.2f}；"
                f"研究主题覆盖 {corpus_coverage:.2f}。"
            )
        else:
            score = top_score
            reason = f"前 {len(top_hits)} 条高相关证据的平均主题相关度为 {top_score:.2f}。"
        return EvaluationMetric(
            name="retrieval_relevance",
            score=round(score, 3),
            reason=reason,
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

    def _citation_metric(
        self,
        report: str,
        papers: list[PaperMetadata],
        hits: list[RetrievalHit],
    ) -> EvaluationMetric:
        known_paper_ids = {paper.id for paper in papers}
        linked_hits = sum(hit.paper_id in known_paper_ids for hit in hits)
        source_backed_hits = sum(
            bool(hit.metadata.get("source")) for hit in hits
        )
        titled_hits = sum(hit.title in report for hit in hits)
        hit_count = len(hits)
        score = (
            0.4 * (linked_hits / hit_count if hit_count else 0.0)
            + 0.3 * (source_backed_hits / hit_count if hit_count else 0.0)
            + 0.3 * (titled_hits / hit_count if hit_count else 0.0)
        )

        return EvaluationMetric(
            name="citation_faithfulness",
            score=round(score, 3),
            reason=(
                f"{linked_hits} / {hit_count} 条证据可关联候选论文，"
                f"{source_backed_hits} 条带来源，{titled_hits} 条在报告中被明确列出。"
            ),
        )
