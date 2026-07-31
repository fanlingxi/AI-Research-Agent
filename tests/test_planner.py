from app.agents.planner import PlannerAgent
from app.llms.provider import MockLLMClient


def test_planner_returns_structured_plan() -> None:
    planner = PlannerAgent(llm=MockLLMClient())
    plan = planner.create_plan("GraphRAG for scientific literature review")

    assert plan.objective
    assert len(plan.steps) >= 1
    assert len(plan.tool_calls) >= 1
    assert plan.tool_calls[0].arguments
