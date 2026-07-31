from __future__ import annotations

from app.agents.document_agent import DocumentAgent
from app.agents.planner import PlannerAgent
from app.agents.reasoning_agent import ReasoningAgent
from app.agents.search_agent import SearchAgent
from app.graph.state import ResearchState
from app.schemas.documents import DocumentChunk, PaperMetadata, RetrievalHit
from app.schemas.research import AgentTrace, ResearchPlan, ToolCall, ToolResult
from app.tools.base import ToolRegistry
from app.tools.research_tools import build_default_tool_registry


def planner_node(state: ResearchState) -> ResearchState:
    query = state["query"]
    planner = PlannerAgent()
    plan = planner.create_plan(query)
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

    report_lines = [
        f"# Phase 2 Research Pipeline Result: {state['query']}",
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

    report_lines.extend(["", "## RAG Summary", "", state.get("rag_answer", "")])

    report_lines.extend(
        [
            "",
            "## Phase 2 Notes",
            "",
            "This run validates the research pipeline: search, document normalization, "
            "chunking, embeddings, vector indexing, and RAG retrieval. "
            "Phase 3 will add entity extraction, relation extraction, Neo4j storage, "
            "graph retrieval, and multi-hop GraphRAG reasoning.",
        ]
    )

    return {
        "final_report": "\n".join(report_lines),
        "traces": [
            AgentTrace(
                node="synthesis",
                message="Generated Phase 2 markdown summary.",
                metadata={"tool_results": len(tool_results)},
            )
        ],
    }
