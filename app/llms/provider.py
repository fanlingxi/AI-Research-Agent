from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import Settings, get_settings


class LLMClient:
    """Minimal text-generation interface used by extraction and report services."""

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        raise NotImplementedError

    last_usage: dict[str, int]


@dataclass
class MockLLMClient(LLMClient):
    """Development sentinel; formal knowledge and reports explicitly reject it."""

    provider_name: str = "mock"
    last_usage: dict[str, int] = field(default_factory=dict, init=False)

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        return "mock output is disabled for formal knowledge and reports"


@dataclass
class LangChainChatClient(LLMClient):
    """Thin wrapper around LangChain chat models using OpenAI-compatible APIs."""

    provider_name: str
    model: str
    api_key: str
    base_url: str
    temperature: float = 0.2
    last_usage: dict[str, int] = field(default_factory=dict, init=False)

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
        raw_usage = response.usage_metadata or response.response_metadata.get("token_usage", {})
        self.last_usage = {
            "input_tokens": int(
                raw_usage.get("input_tokens", raw_usage.get("prompt_tokens", 0)) or 0
            ),
            "output_tokens": int(
                raw_usage.get("output_tokens", raw_usage.get("completion_tokens", 0)) or 0
            ),
        }
        return str(response.content)


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    """Create an LLM client from settings.

    Qwen and DeepSeek are used through their OpenAI-compatible endpoints.
    Missing credentials produce the mock sentinel, which formal services reject.
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
