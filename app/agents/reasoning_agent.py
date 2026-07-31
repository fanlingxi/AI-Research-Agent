from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import get_settings
from app.retrieval.rag import build_rag_retriever
from app.retrieval.vector_store import InMemoryVectorStore
from app.schemas.documents import DocumentChunk, RetrievalHit


@dataclass
class ReasoningResult:
    hits: list[RetrievalHit]
    answer: str
    vector_store_provider: str


class ReasoningAgent:
    """Run vector RAG retrieval and generate an evidence summary."""

    def retrieve_and_answer(
        self,
        query: str,
        chunks: list[DocumentChunk],
        top_k: int | None = None,
        vector_store_provider: str | None = None,
    ) -> ReasoningResult:
        settings = get_settings()
        selected_top_k = top_k or settings.retrieval_top_k
        selected_provider = vector_store_provider or settings.vector_store_provider

        try:
            retriever = build_rag_retriever(vector_store_provider=selected_provider)
            provider_name = retriever.vector_store.provider_name
        except Exception:
            retriever = build_rag_retriever(vector_store_provider="memory")
            retriever.vector_store = InMemoryVectorStore()
            provider_name = "memory"

        retriever.index(chunks)
        hits = retriever.search(query=query, top_k=selected_top_k)
        return ReasoningResult(
            hits=hits,
            answer=self._build_answer(query=query, hits=hits),
            vector_store_provider=provider_name,
        )

    def _build_answer(self, query: str, hits: list[RetrievalHit]) -> str:
        if not hits:
            return "暂未检索到相关文档切片。"

        lines = [
            f"向量 RAG 证据摘要：{query}",
            "",
            "最相关证据：",
        ]
        for index, hit in enumerate(hits, start=1):
            snippet = hit.text.replace("\n", " ")[:420]
            lines.append(f"{index}. {hit.title} (分数={hit.score:.3f}) — {snippet}")

        lines.extend(
            [
                "",
                "向量 RAG 解读：",
                "系统已经能够收集候选论文、构造文档切片、写入向量索引，"
                "并为后续报告生成检索证据。GraphRAG 节点会进一步将这些向量证据"
                "与知识图谱路径结合起来，形成更适合科研分析的多跳推理上下文。",
            ]
        )
        return "\n".join(lines)
