from __future__ import annotations

from app.agents.critic_agent import CriticAgent
from app.agents.document_agent import DocumentAgent
from app.agents.graph_reasoning_agent import GraphReasoningAgent
from app.agents.knowledge_agent import KnowledgeAgent
from app.agents.memory_agent import MemoryAgent
from app.agents.planner import PlannerAgent
from app.agents.reasoning_agent import ReasoningAgent
from app.agents.reflection_agent import ReflectionAgent
from app.agents.search_agent import SearchAgent
from app.agents.writer_agent import WriterAgent
from app.evaluation.evaluator import ResearchEvaluator
from app.graph.state import ResearchState
from app.llms.provider import get_llm_client
from app.schemas.documents import DocumentChunk, PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation
from app.schemas.quality import CritiqueResult, EvaluationResult
from app.schemas.research import AgentTrace, ResearchPlan, ToolCall, ToolResult
from app.tools.base import ToolRegistry
from app.tools.research_tools import build_default_tool_registry


def memory_context_node(state: ResearchState) -> ResearchState:
    snapshot = MemoryAgent(enabled=state.get("memory_enabled")).recall(state["query"])
    memory_enabled = state.get("memory_enabled", True)

    return {
        "memory_context": snapshot.summary,
        "memory_records": snapshot.records,
        "traces": [
            AgentTrace(
                node="memory_context",
                message=(
                    "已召回长期记忆上下文。"
                    if memory_enabled
                    else "已关闭长期记忆，跳过召回。"
                ),
                metadata={"records": len(snapshot.records)},
            )
        ],
    }


def planner_node(state: ResearchState) -> ResearchState:
    query = state["query"]
    planner = PlannerAgent()
    plan = planner.create_plan(query, memory_context=state.get("memory_context", ""))
    for tool_call in plan.tool_calls:
        if tool_call.tool_name == "paper_search":
            tool_call.arguments["query"] = query
            tool_call.arguments["limit"] = state.get("paper_limit", 5)
            tool_call.arguments["live_search"] = state.get("live_search", False)

    return {
        "plan": plan.model_dump(),
        "traces": [
            AgentTrace(
                node="planner",
                message="已生成研究计划。",
                metadata={"steps": len(plan.steps), "tool_calls": len(plan.tool_calls)},
            )
        ],
    }


def tool_executor_node(
    state: ResearchState,
    registry: ToolRegistry | None = None,
) -> ResearchState:
    registry = registry or build_default_tool_registry()
    plan = ResearchPlan.model_validate(state["plan"])

    results = []
    for tool_call in plan.tool_calls:
        result = registry.run(ToolCall.model_validate(tool_call))
        results.append(result)

    return {
        "tool_results": results,
        "traces": [
            AgentTrace(
                node="tool_executor",
                message="已执行计划中的工具调用。",
                metadata={"tool_calls": len(results)},
            )
        ],
    }


def search_node(state: ResearchState) -> ResearchState:
    query = state["query"]
    papers = _dedupe_papers(_papers_from_tool_results(state))
    source = "tool_results"
    if not papers:
        papers = _dedupe_papers(
            SearchAgent().search(
                query=query,
                limit=state.get("paper_limit"),
                live_search=state.get("live_search"),
            )
        )
        source = "search_agent"

    return {
        "papers": papers,
        "traces": [
            AgentTrace(
                node="search",
                message="已收集候选论文。",
                metadata={
                    "papers": len(papers),
                    "live_search": state.get("live_search", False),
                    "source": source,
                },
            )
        ],
    }


def document_node(state: ResearchState) -> ResearchState:
    papers = [PaperMetadata.model_validate(paper) for paper in state.get("papers", [])]
    document_agent = DocumentAgent()
    ingestion = document_agent.ingest_pdf_sources(
        sources=state.get("document_sources", []),
        max_pages=state.get("pdf_max_pages"),
    )
    # Explicit sources are user-selected primary evidence, so they lead the report.
    papers = _dedupe_papers(ingestion.papers + papers)
    chunks = document_agent.build_chunks(papers=papers)

    update: ResearchState = {
        "papers": papers,
        "chunks": chunks,
        "traces": [
            AgentTrace(
                node="document",
                message="已将论文与显式 PDF 转换为检索切片。",
                metadata={
                    "papers": len(papers),
                    "chunks": len(chunks),
                    "pdf_sources": len(state.get("document_sources", [])),
                    "ingested_pdfs": len(ingestion.papers),
                    "pdf_errors": len(ingestion.errors),
                },
            )
        ],
    }
    if ingestion.errors:
        update["errors"] = ingestion.errors
    return update


def knowledge_node(state: ResearchState) -> ResearchState:
    chunks = [DocumentChunk.model_validate(chunk) for chunk in state.get("chunks", [])]
    result = KnowledgeAgent().build_and_retrieve(
        query=state["query"],
        chunks=chunks,
        graph_store_provider=state.get("graph_store_provider"),
    )

    return {
        "graph_entities": result.graph.entities,
        "graph_relations": result.graph.relations,
        "graph_paths": result.graph_paths,
        "traces": [
            AgentTrace(
                node="knowledge",
                message="已抽取实体、关系和图谱路径。",
                metadata={
                    "entities": len(result.graph.entities),
                    "relations": len(result.graph.relations),
                    "paths": len(result.graph_paths),
                    "graph_store": result.graph.graph_store_provider,
                    **result.graph.metadata,
                },
            )
        ],
    }


def retrieval_node(state: ResearchState) -> ResearchState:
    chunks = [DocumentChunk.model_validate(chunk) for chunk in state.get("chunks", [])]
    result = ReasoningAgent().retrieve_and_answer(
        query=state["query"],
        chunks=chunks,
        top_k=state.get("top_k"),
        vector_store_provider=state.get("vector_store_provider"),
    )

    return {
        "retrieval_results": result.hits,
        "rag_answer": result.answer,
        "traces": [
            AgentTrace(
                node="retrieval",
                message="已索引文档切片并检索相关证据。",
                metadata={
                    "chunks": len(chunks),
                    "hits": len(result.hits),
                    "vector_store": result.vector_store_provider,
                },
            )
        ],
    }


def graph_reasoning_node(state: ResearchState) -> ResearchState:
    vector_hits = [
        RetrievalHit.model_validate(hit) for hit in state.get("retrieval_results", [])
    ]
    graph_paths = [GraphPath.model_validate(path) for path in state.get("graph_paths", [])]
    result = GraphReasoningAgent().reason(
        query=state["query"],
        vector_hits=vector_hits,
        graph_paths=graph_paths,
    )

    return {
        "graph_paths": result.graph_paths,
        "graphrag_answer": result.answer,
        "traces": [
            AgentTrace(
                node="graph_reasoning",
                message="已融合向量证据和图谱路径。",
                metadata=result.metadata,
            )
        ],
    }


def _papers_from_tool_results(state: ResearchState) -> list[PaperMetadata]:
    papers: list[PaperMetadata] = []
    for raw_result in state.get("tool_results", []):
        result = ToolResult.model_validate(raw_result)
        if result.tool_name != "paper_search" or result.status != "success":
            continue
        papers.extend(
            PaperMetadata.model_validate(paper)
            for paper in result.metadata.get("papers", [])
        )

    return papers


def _dedupe_papers(papers: list[PaperMetadata]) -> list[PaperMetadata]:
    deduped: dict[str, PaperMetadata] = {}
    for paper in papers:
        key = paper.id or f"{paper.source}:{paper.title}".lower()
        deduped.setdefault(key, paper)
    return list(deduped.values())


def synthesis_node(state: ResearchState) -> ResearchState:
    plan = ResearchPlan.model_validate(state["plan"])
    tool_results = [
        ToolResult.model_validate(result) for result in state.get("tool_results", [])
    ]
    papers = [PaperMetadata.model_validate(paper) for paper in state.get("papers", [])]
    chunks = [DocumentChunk.model_validate(chunk) for chunk in state.get("chunks", [])]
    retrieval_results = [
        RetrievalHit.model_validate(hit) for hit in state.get("retrieval_results", [])
    ]
    graph_entities = [
        GraphEntity.model_validate(entity) for entity in state.get("graph_entities", [])
    ]
    graph_relations = [
        GraphRelation.model_validate(relation)
        for relation in state.get("graph_relations", [])
    ]
    graph_paths = [GraphPath.model_validate(path) for path in state.get("graph_paths", [])]
    writer = WriterAgent(llm=get_llm_client())
    writer_draft = writer.draft(
        query=state["query"],
        plan=plan,
        papers=papers,
        retrieval_hits=retrieval_results,
        graph_paths=graph_paths,
    )

    report_lines = [
        f"# Phase 4 Agentic GraphRAG 科研分析结果：{state['query']}",
        "",
        "## 研究目标",
        "",
        plan.objective,
        "",
        "## 研究问题",
        "",
    ]

    report_lines.extend(f"- {question}" for question in plan.research_questions)
    report_lines.extend(["", "## 执行计划", ""])
    report_lines.extend(
        f"- **{step.id} | {_display_agent_name(step.agent)}**: {step.description} "
        f"(预期输出：{step.expected_output})"
        for step in plan.steps
    )
    report_lines.extend(["", "## 工具调用结果", ""])

    if tool_results:
        for result in tool_results:
            report_lines.extend(
                [
                    f"### {result.tool_name} [{result.status}]",
                    "",
                    result.content,
                    "",
                ]
            )
    else:
        report_lines.append("未执行工具调用。")

    report_lines.extend(["", "## 候选论文", ""])
    if papers:
        for paper in papers:
            year = paper.year or "n.d."
            url = f" <{paper.url}>" if paper.url else ""
            report_lines.append(f"- {paper.title} ({year}) [{paper.source}]{url}")
    else:
        report_lines.append("暂未收集到候选论文。")

    report_lines.extend(["", "## 文档处理", ""])
    report_lines.append(f"- 已处理论文数：{len(papers)}")
    report_lines.append(f"- 已创建检索切片数：{len(chunks)}")
    full_text_papers = sum(
        paper.metadata.get("content_kind") == "pdf_full_text" for paper in papers
    )
    full_text_chunks = sum(
        chunk.metadata.get("content_kind") == "pdf_full_text" for chunk in chunks
    )
    report_lines.append(f"- 显式 PDF 全文：{full_text_papers} 篇 / {full_text_chunks} 个切片")
    errors = state.get("errors", [])
    if errors:
        report_lines.extend(["", "### 运行提示", ""])
        report_lines.extend(f"- {error}" for error in errors)

    report_lines.extend(["", "## 检索证据", ""])
    if retrieval_results:
        for index, hit in enumerate(retrieval_results, start=1):
            snippet = hit.text.replace("\n", " ")[:360]
            report_lines.extend(
                [
                    f"### 证据 {index}：{hit.title}",
                    "",
                    f"- 分数：`{hit.score:.3f}`",
                    f"- 切片：`{hit.chunk_id}`",
                    f"- 来源：{hit.metadata.get('source', 'unknown')}",
                    "",
                    snippet,
                    "",
                ]
            )
    else:
        report_lines.append("暂未检索到相关证据。")

    report_lines.extend(["", "## 知识图谱", ""])
    report_lines.append(f"- 抽取实体数：{len(graph_entities)}")
    report_lines.append(f"- 抽取关系数：{len(graph_relations)}")
    report_lines.append(f"- 检索图谱路径数：{len(graph_paths)}")

    if graph_paths:
        report_lines.extend(["", "## 图谱路径", ""])
        for index, path in enumerate(graph_paths, start=1):
            node_names = " -> ".join(node.name for node in path.nodes)
            relation_types = ", ".join(
                _display_relation_type(relation.type) for relation in path.relations
            )
            report_lines.append(
                f"- 路径 {index}: {node_names} "
                f"(关系={relation_types}; 分数={path.score:.2f})"
            )

    report_lines.extend(["", "## 向量 RAG 总结", "", state.get("rag_answer", "")])
    report_lines.extend(["", "## GraphRAG 推理总结", "", state.get("graphrag_answer", "")])
    report_lines.extend(["", writer_draft])

    report_lines.extend(
        [
            "",
            "## Phase 4 说明",
            "",
            "本次运行验证了完整的 Agentic GraphRAG 闭环：资料检索、文档切片、"
            "实体/关系抽取、知识图谱存储、图谱路径检索、向量证据融合、"
            "Critic 评审与 Reflection 修订。"
            + (
                "已启用长期记忆读写。"
                if state.get("memory_enabled", True)
                else "本次未启用长期记忆。"
            ),
        ]
    )

    return {
        "final_report": "\n".join(report_lines),
        "traces": [
            AgentTrace(
                node="synthesis",
                message="Writer Agent 已生成证据约束的 GraphRAG Markdown 报告。",
                metadata={
                    "tool_results": len(tool_results),
                    "writer_provider": getattr(writer.llm, "provider_name", "unknown"),
                },
            )
        ],
    }


def critic_node(state: ResearchState) -> ResearchState:
    papers = [PaperMetadata.model_validate(paper) for paper in state.get("papers", [])]
    retrieval_results = [
        RetrievalHit.model_validate(hit) for hit in state.get("retrieval_results", [])
    ]
    graph_entities = [
        GraphEntity.model_validate(entity) for entity in state.get("graph_entities", [])
    ]
    graph_relations = [
        GraphRelation.model_validate(relation)
        for relation in state.get("graph_relations", [])
    ]
    graph_paths = [GraphPath.model_validate(path) for path in state.get("graph_paths", [])]

    evaluation = ResearchEvaluator().evaluate(
        query=state["query"],
        report=state.get("final_report", ""),
        papers=papers,
        retrieval_hits=retrieval_results,
        graph_entities=graph_entities,
        graph_relations=graph_relations,
        graph_paths=graph_paths,
    )
    critic_llm = get_llm_client()
    critique = CriticAgent(llm=critic_llm).review(
        report=state.get("final_report", ""),
        evaluation=evaluation,
    )

    return {
        "evaluation_result": evaluation,
        "critique_result": critique,
        "traces": [
            AgentTrace(
                node="critic",
                message="已评估报告质量并生成 Critic 审查意见。",
                metadata={
                    "overall_score": evaluation.overall_score,
                    "passed": evaluation.passed,
                    "needs_revision": critique.needs_revision,
                    "critic_provider": getattr(critic_llm, "provider_name", "unknown"),
                },
            )
        ],
    }


def reflection_node(state: ResearchState) -> ResearchState:
    evaluation = EvaluationResult.model_validate(state["evaluation_result"])
    critique = CritiqueResult.model_validate(state["critique_result"])
    llm = get_llm_client()
    reflection_agent = ReflectionAgent(writer=WriterAgent(llm=llm))
    reflection = reflection_agent.revise(
        report=state.get("final_report", ""),
        evaluation=evaluation,
        critique=critique,
        query=state["query"],
        include_audit=False,
    )
    papers = [PaperMetadata.model_validate(paper) for paper in state.get("papers", [])]
    retrieval_results = [
        RetrievalHit.model_validate(hit) for hit in state.get("retrieval_results", [])
    ]
    graph_entities = [
        GraphEntity.model_validate(entity) for entity in state.get("graph_entities", [])
    ]
    graph_relations = [
        GraphRelation.model_validate(relation)
        for relation in state.get("graph_relations", [])
    ]
    graph_paths = [GraphPath.model_validate(path) for path in state.get("graph_paths", [])]
    final_evaluation = ResearchEvaluator().evaluate(
        query=state["query"],
        report=reflection.revised_report,
        papers=papers,
        retrieval_hits=retrieval_results,
        graph_entities=graph_entities,
        graph_relations=graph_relations,
        graph_paths=graph_paths,
    )
    if critique.needs_revision:
        final_critique = CriticAgent(llm=llm).review(
            report=reflection.revised_report,
            evaluation=final_evaluation,
        )
    else:
        final_critique = critique
    final_report = reflection_agent.append_quality_audit(
        report=reflection.revised_report,
        evaluation=final_evaluation,
        critique=final_critique,
    )

    return {
        "final_report": final_report,
        "evaluation_result": final_evaluation,
        "critique_result": final_critique,
        "reflection_result": reflection,
        "traces": [
            AgentTrace(
                node="reflection",
                message="已完成一次受控 Writer 修订并复评最终报告。",
                metadata={
                    "applied_suggestions": len(reflection.applied_suggestions),
                    "final_score": final_evaluation.overall_score,
                    "final_passed": final_evaluation.passed,
                    "final_needs_revision": final_critique.needs_revision,
                    "writer_provider": getattr(llm, "provider_name", "unknown"),
                },
            )
        ],
    }


def memory_write_node(state: ResearchState) -> ResearchState:
    evaluation = EvaluationResult.model_validate(state["evaluation_result"])
    graph_entities = [
        GraphEntity.model_validate(entity) for entity in state.get("graph_entities", [])
    ]
    tags = [entity.name for entity in graph_entities[:6]]
    record = MemoryAgent(enabled=state.get("memory_enabled")).remember(
        query=state["query"],
        report=state.get("final_report", ""),
        evaluation=evaluation,
        tags=tags,
    )

    return {
        "memory_record": record,
        "traces": [
            AgentTrace(
                node="memory_write",
                message=(
                    "已写入长期记忆。"
                    if record is not None
                    else "已关闭长期记忆，跳过写入。"
                ),
                metadata={"saved": record is not None},
            )
        ],
    }


def _display_agent_name(agent: str) -> str:
    names = {
        "planner": "规划智能体",
        "planner agent": "规划智能体",
        "search": "搜索智能体",
        "search agent": "搜索智能体",
        "document": "文档智能体",
        "document agent": "文档智能体",
        "knowledge": "知识智能体",
        "knowledge agent": "知识智能体",
        "reasoning": "推理智能体",
        "reasoning agent": "推理智能体",
        "writer": "写作智能体",
        "writer agent": "写作智能体",
        "critic": "审查智能体",
        "critic agent": "审查智能体",
    }
    return names.get(agent.strip().lower(), agent)


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
