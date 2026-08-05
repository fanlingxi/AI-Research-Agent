from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.llms.provider import LLMClient, get_llm_client
from app.schemas.research import ResearchPlan, ResearchPlanStep, ToolCall

PLANNER_SYSTEM_PROMPT = """你是 AI Research Agent 系统中的 Planner Agent。

请根据用户输入的研究主题，生成一个结构化 JSON 研究计划。
除 tool_name、agent 这类固定字段外，自然语言内容必须优先使用中文。

JSON schema:

{
  "objective": "string",
  "research_questions": ["string"],
  "steps": [
    {
      "id": "S1",
      "description": "string",
      "agent": "Planner/Search/Document/Knowledge/Reasoning/Writer/Critic Agent",
      "expected_output": "string"
    }
  ],
  "tool_calls": [
    {
      "tool_name": "topic_keyword_expander or paper_search",
      "arguments": {"topic or query": "string"},
      "purpose": "string"
    }
  ]
}

只能使用这些 Phase 4 初始工具：
- topic_keyword_expander(topic: str)
- paper_search(query: str, limit: int, live_search: bool)

只返回 JSON，不要输出解释性文本。
"""


class PlannerAgent:
    """Create a structured research plan and initial tool calls."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or get_llm_client()

    def create_plan(self, query: str, memory_context: str = "") -> ResearchPlan:
        prompt = f"{PLANNER_SYSTEM_PROMPT}\n\n用户研究主题：{query}"
        if memory_context:
            prompt = f"{prompt}\n\n长期记忆上下文：\n{memory_context}"

        try:
            raw_response = self.llm.invoke(prompt)
        except Exception:
            return self._fallback_plan(query)

        try:
            payload = self._extract_json(raw_response)
            plan = ResearchPlan.model_validate(payload)
            return self._normalize_tool_arguments(plan=plan, query=query)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
            return self._fallback_plan(query)

    def _extract_json(self, text: str) -> dict:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?", "", cleaned)
            cleaned = re.sub(r"```$", "", cleaned).strip()

        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)

        return json.loads(cleaned)

    def _normalize_tool_arguments(self, plan: ResearchPlan, query: str) -> ResearchPlan:
        normalized_calls: list[ToolCall] = []
        seen_tool_names: set[str] = set()
        for call in plan.tool_calls:
            arguments = dict(call.arguments)
            if call.tool_name == "topic_keyword_expander":
                tool_name = "topic_keyword_expander"
                arguments["topic"] = query
            elif call.tool_name in {"mock_paper_search", "paper_search"}:
                tool_name = "paper_search"
                arguments["query"] = query
                arguments.setdefault("limit", 5)
                arguments.setdefault("live_search", False)
            else:
                continue

            if tool_name in seen_tool_names:
                continue
            seen_tool_names.add(tool_name)
            normalized_calls.append(
                ToolCall(
                    tool_name=tool_name,
                    arguments=arguments,
                    purpose=call.purpose,
                )
            )

        if not normalized_calls:
            return self._fallback_plan(query)

        plan.tool_calls = normalized_calls
        return plan

    def _fallback_plan(self, query: str) -> ResearchPlan:
        return ResearchPlan(
            objective=f"围绕主题进行科研资料检索、GraphRAG 分析和结构化总结：{query}",
            research_questions=[
                "该主题的核心概念、技术背景和研究脉络是什么？",
                "哪些论文、系统和基准最值得重点分析？",
                "当前方法有哪些主要局限，未来有哪些值得探索的方向？",
            ],
            steps=[
                ResearchPlanStep(
                    id="S1",
                    description="将研究主题扩展为具体检索关键词。",
                    agent="Search Agent",
                    expected_output="检索关键词集合",
                ),
                ResearchPlanStep(
                    id="S2",
                    description="收集候选论文和技术资料。",
                    agent="Search Agent",
                    expected_output="候选资料列表",
                ),
                ResearchPlanStep(
                    id="S3",
                    description="为后续 GraphRAG 实体抽取、关系抽取和图谱推理准备结构。",
                    agent="Knowledge Agent",
                    expected_output="实体与关系抽取清单",
                ),
                ResearchPlanStep(
                    id="S4",
                    description="基于检索证据和图谱路径生成研究报告大纲。",
                    agent="Writer Agent",
                    expected_output="Markdown 报告大纲",
                ),
            ],
            tool_calls=[
                ToolCall(
                    tool_name="topic_keyword_expander",
                    arguments={"topic": query},
                    purpose="生成检索关键词。",
                ),
                ToolCall(
                    tool_name="paper_search",
                    arguments={"query": query, "limit": 5, "live_search": False},
                    purpose="收集候选论文以验证研究流水线。",
                ),
            ],
        )
