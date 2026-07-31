from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.llms.provider import LLMClient, get_llm_client
from app.schemas.research import ResearchPlan, ResearchPlanStep, ToolCall

PLANNER_SYSTEM_PROMPT = """You are the Planner Agent in an AI research system.

Given a user research topic, produce a JSON plan with this schema:

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

Use only these Phase 2 tools:
- topic_keyword_expander(topic: str)
- paper_search(query: str, limit: int, live_search: bool)

Return JSON only.
"""


class PlannerAgent:
    """Create a structured research plan and initial tool calls."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or get_llm_client()

    def create_plan(self, query: str) -> ResearchPlan:
        prompt = f"{PLANNER_SYSTEM_PROMPT}\n\nUser research topic: {query}"
        raw_response = self.llm.invoke(prompt)

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
        for call in plan.tool_calls:
            arguments = dict(call.arguments)
            if call.tool_name == "topic_keyword_expander":
                arguments["topic"] = query
            if call.tool_name in {"mock_paper_search", "paper_search"}:
                tool_name = "paper_search"
                arguments["query"] = query
                arguments.setdefault("limit", 5)
                arguments.setdefault("live_search", False)
            else:
                tool_name = call.tool_name
            normalized_calls.append(
                ToolCall(
                    tool_name=tool_name,
                    arguments=arguments,
                    purpose=call.purpose,
                )
            )
        plan.tool_calls = normalized_calls
        return plan

    def _fallback_plan(self, query: str) -> ResearchPlan:
        return ResearchPlan(
            objective=f"Research and synthesize the topic: {query}",
            research_questions=[
                "What are the key concepts and technical background?",
                "Which papers, systems, and benchmarks are most relevant?",
                "What are the main limitations and promising future directions?",
            ],
            steps=[
                ResearchPlanStep(
                    id="S1",
                    description="Expand the research topic into concrete search keywords.",
                    agent="Search Agent",
                    expected_output="Search keyword set",
                ),
                ResearchPlanStep(
                    id="S2",
                    description="Collect candidate papers and technical sources.",
                    agent="Search Agent",
                    expected_output="Candidate source list",
                ),
                ResearchPlanStep(
                    id="S3",
                    description="Prepare a structure for later GraphRAG extraction and reasoning.",
                    agent="Knowledge Agent",
                    expected_output="Entity and relation extraction checklist",
                ),
                ResearchPlanStep(
                    id="S4",
                    description="Draft a research report outline from the collected evidence.",
                    agent="Writer Agent",
                    expected_output="Markdown report outline",
                ),
            ],
            tool_calls=[
                ToolCall(
                    tool_name="topic_keyword_expander",
                    arguments={"topic": query},
                    purpose="Generate search keywords.",
                ),
                ToolCall(
                    tool_name="paper_search",
                    arguments={"query": query, "limit": 5, "live_search": False},
                    purpose="Collect candidate papers for research pipeline validation.",
                ),
            ],
        )
