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
from app.evaluation.evaluator import ResearchEvaluator
from app.graph.state import ResearchState
from app.schemas.documents import DocumentChunk, PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphPath, GraphRelation
from app.schemas.quality import CritiqueResult, EvaluationResult
from app.schemas.research import AgentTrace, ResearchPlan, ToolCall, ToolResult
from app.tools.base import ToolRegistry
from app.tools.research_tools import build_default_tool_registry


def memory_context_node(state: ResearchState) -> ResearchState:
    snapshot = MemoryAgent(enabled=state.get("memory_enabled")).recall(state["query"])

    return {
        "memory_context": snapshot.summary,
        "memory_records": snapshot.records,
        "traces": [
            AgentTrace(
                node="memory_context",
                message="Retrieved long-term memory context.",
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
                message="Created research plan.",
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
                message="Executed planned tool calls.",
                metadata={"tool_calls": len(results)},
            )
        ],
    }


def search_node(state: ResearchState) -> ResearchState:
    query = state["query"]
    papers = _papers_from_tool_results(state)
    source = "tool_results"
    if not papers:
        papers = SearchAgent().search(
            query=query,
            limit=state.get("paper_limit"),
            live_search=state.get("live_search"),
        )
        source = "search_agent"

    return {
        "papers": papers,
        "traces": [
            AgentTrace(
                node="search",
                message="Collected candidate papers.",
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
    chunks = DocumentAgent().build_chunks(papers=papers)

    return {
        "chunks": chunks,
        "traces": [
            AgentTrace(
                node="document",
                message="Converted papers into retrieval chunks.",
                metadata={"papers": len(papers), "chunks": len(chunks)},
            )
        ],
    }


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
                message="Extracted entities, relations, and graph paths.",
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
                message="Indexed chunks and retrieved relevant evidence.",
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
                message="Synthesized vector evidence and graph paths.",
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

    report_lines = [
        f"# Phase 4 Agentic GraphRAG Research Result: {state['query']}",
        "",
        "## Objective",
        "",
        plan.objective,
        "",
        "## Research Questions",
        "",
    ]

    report_lines.extend(f"- {question}" for question in plan.research_questions)
    report_lines.extend(["", "## Planned Steps", ""])
    report_lines.extend(
        f"- **{step.id} | {step.agent}**: {step.description} "
        f"(Expected: {step.expected_output})"
        for step in plan.steps
    )
    report_lines.extend(["", "## Tool Results", ""])

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
        report_lines.append("No tools were executed.")

    report_lines.extend(["", "## Candidate Papers", ""])
    if papers:
        for paper in papers:
            year = paper.year or "n.d."
            url = f" <{paper.url}>" if paper.url else ""
            report_lines.append(f"- {paper.title} ({year}) [{paper.source}]{url}")
    else:
        report_lines.append("No papers were collected.")

    report_lines.extend(["", "## Document Processing", ""])
    report_lines.append(f"- Papers processed: {len(papers)}")
    report_lines.append(f"- Retrieval chunks created: {len(chunks)}")

    report_lines.extend(["", "## Retrieved Evidence", ""])
    if retrieval_results:
        for index, hit in enumerate(retrieval_results, start=1):
            snippet = hit.text.replace("\n", " ")[:360]
            report_lines.extend(
                [
                    f"### Evidence {index}: {hit.title}",
                    "",
                    f"- Score: `{hit.score:.3f}`",
                    f"- Chunk: `{hit.chunk_id}`",
                    f"- Source: {hit.metadata.get('source', 'unknown')}",
                    "",
                    snippet,
                    "",
                ]
            )
    else:
        report_lines.append("No evidence was retrieved.")

    report_lines.extend(["", "## Knowledge Graph", ""])
    report_lines.append(f"- Entities extracted: {len(graph_entities)}")
    report_lines.append(f"- Relations extracted: {len(graph_relations)}")
    report_lines.append(f"- Graph paths retrieved: {len(graph_paths)}")

    if graph_paths:
        report_lines.extend(["", "## Graph Paths", ""])
        for index, path in enumerate(graph_paths, start=1):
            node_names = " -> ".join(node.name for node in path.nodes)
            relation_types = ", ".join(relation.type for relation in path.relations)
            report_lines.append(
                f"- Path {index}: {node_names} "
                f"(relations={relation_types}; score={path.score:.2f})"
            )

    report_lines.extend(["", "## Vector RAG Summary", "", state.get("rag_answer", "")])
    report_lines.extend(["", "## GraphRAG Summary", "", state.get("graphrag_answer", "")])

    report_lines.extend(
        [
            "",
            "## Phase 4 Notes",
            "",
            "This run validates GraphRAG: entity extraction, relation extraction, "
            "knowledge graph storage, graph path retrieval, and synthesis over both "
            "vector evidence and graph structure. Phase 4 adds long-term memory, "
            "critic feedback, reflection, and evaluation metrics.",
        ]
    )

    return {
        "final_report": "\n".join(report_lines),
        "traces": [
            AgentTrace(
                node="synthesis",
                message="Generated Phase 4 GraphRAG markdown summary.",
                metadata={"tool_results": len(tool_results)},
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
        report=state.get("final_report", ""),
        papers=papers,
        retrieval_hits=retrieval_results,
        graph_entities=graph_entities,
        graph_relations=graph_relations,
        graph_paths=graph_paths,
    )
    critique = CriticAgent().review(report=state.get("final_report", ""), evaluation=evaluation)

    return {
        "evaluation_result": evaluation,
        "critique_result": critique,
        "traces": [
            AgentTrace(
                node="critic",
                message="Evaluated report quality and generated critique.",
                metadata={
                    "overall_score": evaluation.overall_score,
                    "passed": evaluation.passed,
                    "needs_revision": critique.needs_revision,
                },
            )
        ],
    }


def reflection_node(state: ResearchState) -> ResearchState:
    evaluation = EvaluationResult.model_validate(state["evaluation_result"])
    critique = CritiqueResult.model_validate(state["critique_result"])
    reflection = ReflectionAgent().revise(
        report=state.get("final_report", ""),
        evaluation=evaluation,
        critique=critique,
    )

    return {
        "final_report": reflection.revised_report,
        "reflection_result": reflection,
        "traces": [
            AgentTrace(
                node="reflection",
                message="Applied critic feedback to the final report.",
                metadata={"applied_suggestions": len(reflection.applied_suggestions)},
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
                message="Processed long-term memory write.",
                metadata={"saved": record is not None},
            )
        ],
    }
