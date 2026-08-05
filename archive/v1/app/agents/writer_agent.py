from __future__ import annotations

from app.llms.provider import LLMClient, MockLLMClient
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphPath
from app.schemas.quality import CritiqueResult
from app.schemas.research import ResearchPlan

WRITER_SYSTEM_PROMPT = """你是严谨的科研报告 Writer Agent。
只使用提供的论文、检索证据和图谱路径，不得编造实验、数据集、结论或引用。
输出简洁的中文 Markdown；每个核心结论要用 [1]、[2] 形式引用给定证据编号。
报告必须包含“核心发现”“机制分析”“局限性”“结论与下一步”四个二级标题。
"""


class WriterAgent:
    """Generate and revise evidence-grounded research narrative sections."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or MockLLMClient()

    def draft(
        self,
        query: str,
        plan: ResearchPlan,
        papers: list[PaperMetadata],
        retrieval_hits: list[RetrievalHit],
        graph_paths: list[GraphPath],
    ) -> str:
        fallback = self._fallback_draft(query, papers, retrieval_hits, graph_paths)
        if isinstance(self.llm, MockLLMClient):
            return fallback

        prompt = self._draft_prompt(query, plan, papers, retrieval_hits, graph_paths)
        try:
            response = self.llm.invoke(prompt, system_prompt=WRITER_SYSTEM_PROMPT)
        except Exception:
            return fallback
        return response.strip() if self._is_usable(response) else fallback

    def revise(
        self,
        report: str,
        critique: CritiqueResult,
        query: str,
    ) -> str:
        if not critique.needs_revision:
            return report

        fallback = self._fallback_revision(report, critique)
        if isinstance(self.llm, MockLLMClient):
            return fallback

        prompt = "\n\n".join(
            [
                "研究主题：" + query,
                "当前报告：\n" + report[:12000],
                "必须处理的审查问题：\n" + self._bullet_list(critique.issues),
                "修订建议：\n" + self._bullet_list(critique.suggestions),
                "请在不编造证据的前提下重写报告中的研究报告叙事；保留已有来源编号和所有事实性证据。",
            ]
        )
        try:
            response = self.llm.invoke(prompt, system_prompt=WRITER_SYSTEM_PROMPT)
        except Exception:
            return fallback
        if not self._is_usable(response):
            return fallback
        return "\n".join(
            [
                report.rstrip(),
                "",
                "## 修订后的研究报告",
                "",
                response.strip(),
            ]
        )

    def _draft_prompt(
        self,
        query: str,
        plan: ResearchPlan,
        papers: list[PaperMetadata],
        hits: list[RetrievalHit],
        paths: list[GraphPath],
    ) -> str:
        paper_lines = [
            f"[{index}] {paper.title} | {paper.year or 'n.d.'} | {paper.abstract[:500]}"
            for index, paper in enumerate(papers[:8], start=1)
        ]
        evidence_lines = [
            f"[{index}] {hit.title} | 来源={hit.metadata.get('source', 'unknown')} | "
            f"{hit.text[:700]}"
            for index, hit in enumerate(hits[:5], start=1)
        ]
        path_lines = [
            f"- {' -> '.join(node.name for node in path.nodes)}"
            for path in paths[:5]
        ]
        return "\n\n".join(
            [
                f"研究主题：{query}",
                f"研究目标：{plan.objective}",
                "候选论文：\n" + self._bullet_list(paper_lines),
                "检索证据：\n" + self._bullet_list(evidence_lines),
                "图谱路径：\n" + self._bullet_list(path_lines),
                "请生成可直接放入研究报告的分析正文。",
            ]
        )

    def _fallback_draft(
        self,
        query: str,
        papers: list[PaperMetadata],
        hits: list[RetrievalHit],
        paths: list[GraphPath],
    ) -> str:
        evidence_titles = [hit.title for hit in hits[:3]] or [
            paper.title for paper in papers[:3]
        ]
        citations = "、".join(
            f"[{index + 1}] {title}" for index, title in enumerate(evidence_titles)
        )
        path_summary = (
            " -> ".join(node.name for node in paths[0].nodes)
            if paths
            else "尚未检索到稳定的图谱路径"
        )
        return "\n".join(
            [
                "## 研究报告",
                "",
                "### 核心发现",
                f"围绕“{query}”，当前证据集中于：{citations}。",
                "",
                "### 机制分析",
                f"最具代表性的图谱连接为：{path_summary}。该路径可用于将文献、方法和检索证据组织为可追溯的推理上下文。",
                "",
                "### 局限性",
                "当前结论受候选论文覆盖范围、摘要信息完整度和图谱实体抽取质量限制，应避免将检索排序直接解释为因果结论。",
                "",
                "### 结论与下一步",
                "建议补充领域内基准论文与全文证据，再比较不同检索策略、图谱路径质量和引用覆盖度。",
            ]
        )

    def _fallback_revision(self, report: str, critique: CritiqueResult) -> str:
        suggestions = self._bullet_list(critique.suggestions) or "- 当前证据不足，需人工复核。"
        return "\n".join(
            [
                report.rstrip(),
                "",
                "## 修订说明",
                "本次自动修订保留原始证据，并将以下风险显式纳入结论边界：",
                suggestions,
            ]
        )

    def _bullet_list(self, items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    def _is_usable(self, response: str) -> bool:
        return len(response.strip()) >= 160 and "##" in response
