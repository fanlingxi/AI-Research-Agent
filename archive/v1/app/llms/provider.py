from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import Settings, get_settings


class LLMClient:
    """Minimal text-generation interface used by agents."""

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        raise NotImplementedError


@dataclass
class MockLLMClient(LLMClient):
    """Deterministic local fallback so the workflow can run without API keys."""

    provider_name: str = "mock"

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        return """
{
  "objective": "围绕用户给定主题制定一份可执行的科研分析计划。",
  "research_questions": [
    "该主题的核心概念、定义和研究背景是什么？",
    "哪些论文、系统或基准最值得优先分析？",
    "需要比较哪些方法、局限性和未来研究方向？"
  ],
  "steps": [
    {
      "id": "S1",
      "description": "将研究主题扩展为检索关键词和子问题。",
      "agent": "Search Agent",
      "expected_output": "关键词集合和检索策略"
    },
    {
      "id": "S2",
      "description": "收集候选论文、技术报告和相关资料。",
      "agent": "Search Agent",
      "expected_output": "候选资料列表"
    },
    {
      "id": "S3",
      "description": "抽取实体、方法、数据集、指标和关系。",
      "agent": "Knowledge Agent",
      "expected_output": "初始知识图谱结构"
    },
    {
      "id": "S4",
      "description": "结合向量证据和图谱路径生成结构化研究报告。",
      "agent": "Writer Agent",
      "expected_output": "Markdown 研究报告大纲"
    }
  ],
  "tool_calls": [
    {
      "tool_name": "topic_keyword_expander",
      "arguments": {"topic": "user topic"},
      "purpose": "为研究主题生成检索关键词"
    },
    {
      "tool_name": "paper_search",
      "arguments": {"query": "user topic", "limit": 5, "live_search": false},
      "purpose": "为研究主题收集候选论文"
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

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
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
                    content=system_prompt
                    or (
                        "你是一名资深 AI 科研规划智能体。"
                        "请始终返回符合指定 schema 的合法 JSON，"
                        "并且 JSON 中的自然语言内容优先使用中文。"
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
