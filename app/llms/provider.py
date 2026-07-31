from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings, get_settings


class LLMClient:
    """Minimal text-generation interface used by agents."""

    def invoke(self, prompt: str) -> str:
        raise NotImplementedError


@dataclass
class MockLLMClient(LLMClient):
    """Deterministic local fallback so the workflow can run without API keys."""

    provider_name: str = "mock"

    def invoke(self, prompt: str) -> str:
        return """
{
  "objective": "Create an initial research plan for the requested topic.",
  "research_questions": [
    "What are the core concepts and definitions?",
    "What are the most relevant papers, systems, or benchmarks?",
    "What methods, limitations, and future directions should be compared?"
  ],
  "steps": [
    {
      "id": "S1",
      "description": "Expand the topic into search keywords and subtopics.",
      "agent": "Search Agent",
      "expected_output": "Keyword set and search strategy"
    },
    {
      "id": "S2",
      "description": "Collect candidate papers and technical resources.",
      "agent": "Search Agent",
      "expected_output": "Candidate source list"
    },
    {
      "id": "S3",
      "description": "Extract entities, methods, datasets, and relationships.",
      "agent": "Knowledge Agent",
      "expected_output": "Initial knowledge schema"
    },
    {
      "id": "S4",
      "description": "Synthesize evidence into a structured research report.",
      "agent": "Writer Agent",
      "expected_output": "Markdown report outline"
    }
  ],
  "tool_calls": [
    {
      "tool_name": "topic_keyword_expander",
      "arguments": {"topic": "user topic"},
      "purpose": "Generate search keywords for the research topic"
    },
    {
      "tool_name": "mock_paper_search",
      "arguments": {"query": "user topic", "limit": 5},
      "purpose": "Return placeholder paper candidates for Phase 1"
    }
  ]
}
""".strip()


@dataclass
class LangChainChatClient(LLMClient):
    """Thin wrapper around LangChain chat models using OpenAI-compatible APIs."""

    provider_name: str
    model: str
    api_key: str
    base_url: str
    temperature: float = 0.2

    def invoke(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        chat = ChatOpenAI(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
        )
        response = chat.invoke(
            [
                SystemMessage(
                    content=(
                        "You are a senior AI research planning agent. "
                        "Always return valid JSON that follows the requested schema."
                    )
                ),
                HumanMessage(content=prompt),
            ]
        )
        return str(response.content)


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Create an LLM client from settings.

    Qwen and DeepSeek are used through their OpenAI-compatible endpoints.
    If provider credentials are missing, the client falls back to mock mode.
    """

    settings = settings or get_settings()

    if settings.llm_provider == "openai" and settings.openai_api_key:
        return LangChainChatClient(
            provider_name="openai",
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            temperature=settings.llm_temperature,
        )

    if settings.llm_provider == "qwen" and settings.qwen_api_key:
        return LangChainChatClient(
            provider_name="qwen",
            model=settings.qwen_model,
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            temperature=settings.llm_temperature,
        )

    if settings.llm_provider == "deepseek" and settings.deepseek_api_key:
        return LangChainChatClient(
            provider_name="deepseek",
            model=settings.deepseek_model,
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            temperature=settings.llm_temperature,
        )

    return MockLLMClient()
