from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.config.settings import get_settings
from app.graph.nodes import (
    critic_node,
    document_node,
    graph_reasoning_node,
    knowledge_node,
    memory_context_node,
    memory_write_node,
    planner_node,
    reflection_node,
    retrieval_node,
    search_node,
    synthesis_node,
    tool_executor_node,
)
from app.graph.state import ResearchState


def build_workflow():
    """Build the Phase 4 LangGraph workflow."""

    graph = StateGraph(ResearchState)
    graph.add_node("memory_context", memory_context_node)
    graph.add_node("planner", planner_node)
    graph.add_node("tool_executor", tool_executor_node)
    graph.add_node("search", search_node)
    graph.add_node("document", document_node)
    graph.add_node("knowledge", knowledge_node)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("graph_reasoning", graph_reasoning_node)
    graph.add_node("synthesis", synthesis_node)
    graph.add_node("critic", critic_node)
    graph.add_node("reflection", reflection_node)
    graph.add_node("memory_write", memory_write_node)

    graph.set_entry_point("memory_context")
    graph.add_edge("memory_context", "planner")
    graph.add_edge("planner", "tool_executor")
    graph.add_edge("tool_executor", "search")
    graph.add_edge("search", "document")
    graph.add_edge("document", "knowledge")
    graph.add_edge("knowledge", "retrieval")
    graph.add_edge("retrieval", "graph_reasoning")
    graph.add_edge("graph_reasoning", "synthesis")
    graph.add_edge("synthesis", "critic")
    graph.add_edge("critic", "reflection")
    graph.add_edge("reflection", "memory_write")
    graph.add_edge("memory_write", END)

    return graph.compile()


def run_research_workflow(
    query: str,
    live_search: bool | None = None,
    paper_limit: int | None = None,
    top_k: int | None = None,
    vector_store_provider: str | None = None,
    graph_store_provider: str | None = None,
    memory_enabled: bool | None = None,
    document_sources: list[str] | None = None,
    pdf_max_pages: int | None = None,
) -> ResearchState:
    """Run the Phase 4 workflow for a user research query."""

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
            "memory_enabled": settings.memory_enabled if memory_enabled is None else memory_enabled,
            "document_sources": document_sources or [],
            "pdf_max_pages": pdf_max_pages,
        }
    )
