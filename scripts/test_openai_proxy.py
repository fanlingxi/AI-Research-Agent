"""Run a redacted OpenAI-compatible proxy smoke test.

The script deliberately reads the credential only from OPENAI_API_KEY and
never accepts it as a command-line option.  It is intended for explicitly
configured, low-cost compatibility checks rather than application traffic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

MODEL_MARKERS = ("nano", "mini", "flash")
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_TOKENS = 32
_CREDENTIAL_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(
        r"(?i)\b(authorization|api[-_ ]?key)\b\s*[:=]\s*"
        r"(?:bearer\s+)?[^\s,;]+"
    ),
)


class SmokeTestError(RuntimeError):
    """Expected proxy smoke-test failure with a safe category."""

    def __init__(self, error_type: str, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.error_type = error_type
        self.status_code = status_code


def normalize_base_url(value: str) -> str:
    """Validate a credential-free HTTP base URL and remove its trailing slash."""

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base URL must be an http(s) URL without credentials, query, or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def select_economy_model(model_ids: Iterable[str]) -> str | None:
    """Pick a deterministic low-cost candidate from the provider's model IDs."""

    unique_ids = sorted({model_id for model_id in model_ids if model_id.strip()}, key=str.casefold)
    for marker in MODEL_MARKERS:
        matches = [model_id for model_id in unique_ids if marker in model_id.casefold()]
        if matches:
            return matches[0]
    return None


def build_chat_payload(model: str, *, max_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly PONG."}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }


def redact_text(value: str) -> str:
    """Prevent accidental credential echoing from error bodies or exceptions."""

    redacted = value
    redacted = _CREDENTIAL_PATTERNS[0].sub("[REDACTED]", redacted)
    redacted = _CREDENTIAL_PATTERNS[1].sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted


def _response_detail(response: httpx.Response) -> str:
    try:
        body = json.dumps(response.json(), ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        body = response.text
    return redact_text(body[:500])


def _model_ids(response: httpx.Response) -> list[str]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise SmokeTestError("models_invalid_json", "GET /models did not return JSON") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SmokeTestError("models_invalid_shape", "GET /models did not return a data array")
    return [
        item["id"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def _response_content(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise SmokeTestError(
            "chat_invalid_json", "POST /chat/completions did not return JSON"
        ) from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (IndexError, KeyError, TypeError) as exc:
        raise SmokeTestError(
            "chat_invalid_shape", "POST /chat/completions returned no assistant message"
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise SmokeTestError(
            "chat_empty_content", "POST /chat/completions returned an empty assistant message"
        )
    return content


def run_smoke_test(
    base_url: str,
    *,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Discover an economy model and issue one bounded Chat Completions request."""

    normalized_url = normalize_base_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}
    started = time.perf_counter()
    with httpx.Client(timeout=timeout_seconds) as client:
        try:
            models_response = client.get(f"{normalized_url}/models", headers=headers)
        except httpx.HTTPError as exc:
            raise SmokeTestError("models_transport_error", redact_text(str(exc))) from exc
        if not models_response.is_success:
            raise SmokeTestError(
                "models_http_error",
                _response_detail(models_response),
                status_code=models_response.status_code,
            )
        model_ids = _model_ids(models_response)
        selected_model = select_economy_model(model_ids)
        if selected_model is None:
            raise SmokeTestError(
                "economy_model_unavailable",
                "GET /models returned no model ID containing nano, mini, or flash",
            )

        try:
            chat_response = client.post(
                f"{normalized_url}/chat/completions",
                headers=headers,
                json=build_chat_payload(selected_model, max_tokens=max_tokens),
            )
        except httpx.HTTPError as exc:
            raise SmokeTestError("chat_transport_error", redact_text(str(exc))) from exc
        if not chat_response.is_success:
            raise SmokeTestError(
                "chat_http_error",
                _response_detail(chat_response),
                status_code=chat_response.status_code,
            )
        _response_content(chat_response)

    candidates = [
        model_id
        for marker in MODEL_MARKERS
        for model_id in sorted(model_ids, key=str.casefold)
        if marker in model_id.casefold()
    ]
    return {
        "ok": True,
        "base_url": normalized_url,
        "selected_model": selected_model,
        "economy_candidates": list(dict.fromkeys(candidates)),
        "model_count": len(model_ids),
        "max_tokens": max_tokens,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="OpenAI-compatible API base URL")
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout"
    )
    parser.add_argument(
        "--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Completion token cap"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    if args.timeout_seconds <= 0 or args.max_tokens <= 0:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": "invalid_arguments",
                    "detail": "limits must be positive",
                },
                ensure_ascii=False,
            )
        )
        return 2

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": "api_key_missing",
                    "detail": "set OPENAI_API_KEY in the process environment",
                },
                ensure_ascii=False,
            )
        )
        return 2

    try:
        result = run_smoke_test(
            args.base_url,
            api_key=api_key,
            timeout_seconds=args.timeout_seconds,
            max_tokens=args.max_tokens,
        )
    except ValueError as exc:
        result = {"ok": False, "error_type": "invalid_base_url", "detail": redact_text(str(exc))}
    except SmokeTestError as exc:
        result = {
            "ok": False,
            "base_url": args.base_url,
            "error_type": exc.error_type,
            "detail": redact_text(str(exc)),
        }
        if exc.status_code is not None:
            result["status_code"] = exc.status_code
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
