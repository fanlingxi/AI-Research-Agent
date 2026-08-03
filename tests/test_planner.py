from app.agents.planner import PlannerAgent
from app.graph.nodes import _dedupe_papers
from app.llms.provider import LLMClient, MockLLMClient
from app.schemas.documents import PaperMetadata


def test_planner_returns_structured_plan() -> None:
    planner = PlannerAgent(llm=MockLLMClient())
    plan = planner.create_plan("GraphRAG for scientific literature review")

    assert plan.objective
    assert len(plan.steps) >= 1
    assert len(plan.tool_calls) >= 1
    assert plan.tool_calls[0].arguments


class DuplicateToolLLM(LLMClient):
    def invoke(self, prompt: str) -> str:
        return """
{
  "objective": "测试重复工具调用归一化。",
  "research_questions": ["如何避免重复检索？"],
  "steps": [
    {
      "id": "S1",
      "description": "规划并检索资料。",
      "agent": "Search Agent",
      "expected_output": "候选论文"
    }
  ],
  "tool_calls": [
    {
      "tool_name": "topic_keyword_expander",
      "arguments": {"topic": "old"},
      "purpose": "扩展关键词"
    },
    {
      "tool_name": "paper_search",
      "arguments": {"query": "old", "limit": 3, "live_search": false},
      "purpose": "第一次检索"
    },
    {
      "tool_name": "paper_search",
      "arguments": {"query": "old", "limit": 3, "live_search": false},
      "purpose": "重复检索"
    },
    {
      "tool_name": "unknown_tool",
      "arguments": {},
      "purpose": "不应执行"
    }
  ]
}
""".strip()


def test_planner_deduplicates_and_bounds_tool_calls() -> None:
    planner = PlannerAgent(llm=DuplicateToolLLM())
    plan = planner.create_plan("GraphRAG 在科研文献综述中的应用")

    tool_names = [call.tool_name for call in plan.tool_calls]

    assert tool_names == ["topic_keyword_expander", "paper_search"]
    assert plan.tool_calls[0].arguments["topic"] == "GraphRAG 在科研文献综述中的应用"
    assert plan.tool_calls[1].arguments["query"] == "GraphRAG 在科研文献综述中的应用"


def test_paper_deduplication_keeps_first_unique_paper() -> None:
    first = PaperMetadata(
        id="offline-demo:1",
        title="GraphRAG 在科研文献综述中的应用: 综述基础",
        source="offline-demo",
        year=2026,
    )
    duplicate = first.model_copy(update={"year": 2027})
    second = PaperMetadata(
        id="offline-demo:2",
        title="GraphRAG 在科研文献综述中的应用: 检索与索引",
        source="offline-demo",
        year=2026,
    )

    papers = _dedupe_papers([first, duplicate, second])

    assert [paper.id for paper in papers] == ["offline-demo:1", "offline-demo:2"]
    assert papers[0].year == 2026
