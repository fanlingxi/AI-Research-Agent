from typing import Annotated, Any, TypedDict

from app.schemas.research import AgentTrace, ToolResult


def append_list(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    return (left or []) + (right or [])


class ResearchState(TypedDict, total=False):
    """State passed between LangGraph nodes."""

    query: str
    plan: dict[str, Any]
    tool_results: Annotated[list[ToolResult], append_list]
    traces: Annotated[list[AgentTrace], append_list]
    final_report: str
    errors: Annotated[list[str], append_list]
