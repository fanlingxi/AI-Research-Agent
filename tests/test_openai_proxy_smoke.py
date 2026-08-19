from scripts.test_openai_proxy import (
    build_chat_payload,
    normalize_base_url,
    redact_text,
    select_economy_model,
)


def test_select_economy_model_prioritizes_nano_then_mini_then_flash() -> None:
    assert select_economy_model(["gpt-4.1-mini", "gpt-5-flash", "gpt-5-nano"]) == "gpt-5-nano"
    assert select_economy_model(["qwen-flash", "gpt-4.1-mini"]) == "gpt-4.1-mini"
    assert select_economy_model(["gpt-4.1"]) is None


def test_smoke_payload_is_bounded_chat_completions_request() -> None:
    assert build_chat_payload("gpt-5-nano", max_tokens=32) == {
        "model": "gpt-5-nano",
        "messages": [{"role": "user", "content": "Reply with exactly PONG."}],
        "max_tokens": 32,
        "temperature": 0,
    }


def test_error_text_redacts_common_credential_forms() -> None:
    detail = redact_text("Authorization: Bearer sk-test_123 api_key=sk-second_456")

    assert "sk-test_123" not in detail
    assert "sk-second_456" not in detail
    assert detail.count("[REDACTED]") == 2


def test_normalize_base_url_removes_a_trailing_slash_and_rejects_user_info() -> None:
    assert normalize_base_url("https://proxy.example/v1/") == "https://proxy.example/v1"

    try:
        normalize_base_url("https://token@proxy.example/v1")
    except ValueError as exc:
        assert "without credentials" in str(exc)
    else:
        raise AssertionError("credential-bearing base URLs must be rejected")
