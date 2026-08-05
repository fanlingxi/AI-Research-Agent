from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A structured tool invocation requested by an agent."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str = ""


class ResearchPlanStep(BaseModel):
    """One step in the research plan."""

    id: str
    description: str
    agent: str
    expected_output: str


class ResearchPlan(BaseModel):
    """Planner output consumed by the workflow."""

    objective: str
    research_questions: list[str] = Field(default_factory=list)
    steps: list[ResearchPlanStep] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)


class ToolResult(BaseModel):
    """Normalized result returned by a tool."""

    tool_name: str
    status: Literal["success", "error"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentTrace(BaseModel):
    """Lightweight execution trace for demo and debugging."""

    node: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
