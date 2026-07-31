from __future__ import annotations

from app.schemas.documents import RetrievalHit
from app.schemas.graph import GraphPath, GraphRAGResult


class GraphRAGReasoner:
    """Combine vector evidence and graph paths into a multi-hop answer."""

    def synthesize(
        self,
        query: str,
        vector_hits: list[RetrievalHit],
        graph_paths: list[GraphPath],
    ) -> GraphRAGResult:
        lines = [
            f"GraphRAG 推理摘要：{query}",
            "",
            "向量证据信号：",
        ]

        if vector_hits:
            for index, hit in enumerate(vector_hits[:5], start=1):
                lines.append(f"{index}. {hit.title} (分数={hit.score:.3f})")
        else:
            lines.append("尚未检索到向量证据。")

        lines.extend(["", "图谱多跳信号："])
        if graph_paths:
            for index, path in enumerate(graph_paths[:5], start=1):
                node_names = " -> ".join(node.name for node in path.nodes)
                relation_types = ", ".join(
                    _display_relation_type(relation.type) for relation in path.relations
                )
                lines.append(
                    f"{index}. {node_names} "
                    f"(关系={relation_types}; 分数={path.score:.2f})"
                )
        else:
            lines.append("尚未检索到图谱路径。")

        lines.extend(
            [
                "",
                "综合解读：",
                self._interpretation(vector_hits=vector_hits, graph_paths=graph_paths),
            ]
        )

        return GraphRAGResult(
            graph_paths=graph_paths,
            answer="\n".join(lines),
            metadata={
                "vector_hits": len(vector_hits),
                "graph_paths": len(graph_paths),
            },
        )

    def _interpretation(
        self,
        vector_hits: list[RetrievalHit],
        graph_paths: list[GraphPath],
    ) -> str:
        if not vector_hits and not graph_paths:
            return "当前证据不足，暂时无法形成可靠的 GraphRAG 推理结论。"

        if vector_hits and graph_paths:
            return (
                "当前回答同时受到向量检索证据和图谱路径支撑。向量侧提供语义相关的"
                "文档片段，图谱侧连接论文、概念、方法、数据集和评估信号，使系统"
                "从普通 RAG 进一步升级为具备图增强多跳分析能力的科研 Agent。"
            )

        if vector_hits:
            return (
                "当前回答主要由向量检索支撑。如果预期出现图谱路径，需要扩展实体/关系"
                "抽取范围，或检查 Neo4j/图存储配置。"
            )

        return (
            "当前回答主要由图谱结构支撑。增加文档切片数量或优化 embedding 配置，"
            "可以提升向量证据侧的覆盖度。"
        )


def _display_relation_type(relation_type: str) -> str:
    names = {
        "DISCUSSES": "讨论",
        "CO_OCCURS_WITH": "共现",
        "BUILDS_ON": "基于",
        "EVALUATES": "评估",
        "USES": "使用",
        "COMPARES_WITH": "对比",
    }
    return names.get(relation_type, relation_type)
