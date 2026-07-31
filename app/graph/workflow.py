from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.graph.nodes import planner_node, synthesis_node, tool_executor_node
from app.graph.state import ResearchState


def build_workflow():
    """Build the Phase 1 LangGraph workflow."""

    graph = StateGraph(ResearchState)
    graph.add_node("planner", planner_node)
    graph.add_node("tool_executor", tool_executor_node)
    graph.add_node("synthesis", synthesis_node)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "tool_executor")
    graph.add_edge("tool_executor", "synthesis")
    graph.add_edge("synthesis", END)

    return graph.compile()


def run_research_workflow(query: str) -> ResearchState:
    """Run the Phase 1 workflow for a user research query."""

    workflow = build_workflow()
    return workflow.invoke({"query": query})
