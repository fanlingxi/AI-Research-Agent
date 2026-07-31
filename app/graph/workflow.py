from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.config.settings import get_settings
from app.graph.nodes import (
    document_node,
    graph_reasoning_node,
    knowledge_node,
    planner_node,
    retrieval_node,
    search_node,
    synthesis_node,
    tool_executor_node,
)
from app.graph.state import ResearchState


def build_workflow():
    """Build the Phase 3 LangGraph workflow."""

    graph = StateGraph(ResearchState)
    graph.add_node("planner", planner_node)
    graph.add_node("tool_executor", tool_executor_node)
    graph.add_node("search", search_node)
    graph.add_node("document", document_node)
    graph.add_node("knowledge", knowledge_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("graph_reasoning", graph_reasoning_node)
    graph.add_node("synthesis", synthesis_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "tool_executor")
    graph.add_edge("tool_executor", "search")
    graph.add_edge("search", "document")
    graph.add_edge("document", "knowledge")
    graph.add_edge("knowledge", "retrieval")
    graph.add_edge("retrieval", "graph_reasoning")
    graph.add_edge("graph_reasoning", "synthesis")
    graph.add_edge("synthesis", END)

    return graph.compile()


def run_research_workflow(
    query: str,
    live_search: bool | None = None,
    paper_limit: int | None = None,
    top_k: int | None = None,
    vector_store_provider: str | None = None,
    graph_store_provider: str | None = None,
) -> ResearchState:
    """Run the Phase 3 workflow for a user research query."""

    settings = get_settings()
    workflow = build_workflow()
    return workflow.invoke(
        {
            "query": query,
            "live_search": settings.search_live_enabled if live_search is None else live_search,
            "paper_limit": paper_limit or settings.paper_search_limit,
            "top_k": top_k or settings.retrieval_top_k,
            "vector_store_provider": vector_store_provider or settings.vector_store_provider,
            "graph_store_provider": graph_store_provider or settings.graph_store_provider,
        }
    )
