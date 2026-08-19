from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from app.config.settings import Settings
from app.llms.provider import LangChainChatClient, get_llm_client


def _install_fake_langchain(monkeypatch):
    captured: dict[str, object] = {}

    class _Message:
        def __init__(self, *, content: str) -> None:
            self.content = content

    class _ChatOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def invoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(
                content="ok",
                usage_metadata={"input_tokens": 3, "output_tokens": 5},
                response_metadata={},
            )

    messages_module = ModuleType("langchain_core.messages")
    messages_module.HumanMessage = _Message
    messages_module.SystemMessage = _Message
    openai_module = ModuleType("langchain_openai")
    openai_module.ChatOpenAI = _ChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_core.messages", messages_module)
    monkeypatch.setitem(sys.modules, "langchain_openai", openai_module)
    return captured


def test_client_forwards_legacy_max_tokens_in_proxy_request_body(monkeypatch) -> None:
    captured = _install_fake_langchain(monkeypatch)
    client = LangChainChatClient(
        provider_name="openai",
        model="gpt-5-nano",
        api_key="test-key",
        base_url="https://proxy.example/v1",
        max_tokens=512,
        reasoning_effort="none",
    )

    assert client.invoke("hello") == "ok"
    assert captured["extra_body"] == {"max_tokens": 512}
    assert captured["reasoning_effort"] == "none"
    assert client.last_usage == {"input_tokens": 3, "output_tokens": 5}


def test_client_omits_max_tokens_when_unconfigured(monkeypatch) -> None:
    captured = _install_fake_langchain(monkeypatch)
    client = LangChainChatClient(
        provider_name="openai",
        model="gpt-5-nano",
        api_key="test-key",
        base_url="https://proxy.example/v1",
    )

    client.invoke("hello")

    assert "extra_body" not in captured


def test_factory_passes_max_tokens_from_settings() -> None:
    client = get_llm_client(
        Settings(
            llm_provider="openai",
            openai_api_key="test-key",
            openai_base_url="https://proxy.example/v1",
            llm_max_tokens=512,
            llm_reasoning_effort="none",
        )
    )

    assert isinstance(client, LangChainChatClient)
    assert client.max_tokens == 512
    assert client.reasoning_effort == "none"
