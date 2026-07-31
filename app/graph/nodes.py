from __future__ import annotations

from app.agents.planner import PlannerAgent
from app.graph.state import ResearchState
from app.schemas.research import AgentTrace, ResearchPlan, ToolCall
from app.tools.base import ToolRegistry
from app.tools.research_tools import build_default_tool_registry


def planner_node(state: ResearchState) -> ResearchState:
    query = state["query"]
    planner = PlannerAgent()
    plan = planner.create_plan(query)

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


def synthesis_node(state: ResearchState) -> ResearchState:
    plan = ResearchPlan.model_validate(state["plan"])
    tool_results = state.get("tool_results", [])

    report_lines = [
        f"# Phase 1 Research Workflow Result: {state['query']}",
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

    report_lines.extend(
        [
            "## Phase 1 Notes",
            "",
            "This run validates the basic LangGraph agent framework. "
            "Real search, PDF parsing, vector retrieval, and GraphRAG will be added in later phases.",
        ]
    )

    return {
        "final_report": "\n".join(report_lines),
        "traces": [
            AgentTrace(
                node="synthesis",
                message="Generated Phase 1 markdown summary.",
                metadata={"tool_results": len(tool_results)},
            )
        ],
    }
