"""Bounded diagnostics for durable errors and public API responses."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit

from app.config.settings import Settings, get_settings

REDACTED = "[REDACTED]"
ERROR_MAX_LENGTH = 4000
_SECRET_LABEL = (
    r"(?:(?:[\w.-]+[_.-])?"
    r"(?:api[_ -]?key|access[_ -]?(?:key|token)|secret[_ -]?key|"
    r"token|password|secret|credential|signature|authorization)|key)"
)
_SECRET_NAME = re.compile(_SECRET_LABEL, re.I)
_PLACEHOLDER_SECRETS = frozenset({
    "neo4j", "password", "password123", "changeme", "change-me", "change_me",
    "default", "secret", "your-api-key", "your_api_key", "replace-me", "replace_me",
})
_URL_USERINFO = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/<>\"']*@", re.I)
_AUTH = re.compile(
    r"(\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?:[a-z][a-z0-9_-]*\s+)?[^\s,;\"'{}]+",
    re.I,
)
_BEARER = re.compile(r"(\bbearer\s+)[^\s,;\"'{}]+", re.I)
_ASSIGNMENT = re.compile(
    r"(?P<label>(?<![\w.-])" + _SECRET_LABEL + r")(?![\w.-])"
    r"(?P<sep>[\"']?\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s&\#;,\"'{}]+)",
    re.I,
)
_SK_KEY = re.compile(r"\bsk-[a-z0-9_-]+", re.I)
_QUERY = re.compile(r"([?&])([^\s=&#\"']+)=([^\s&#\"']*)")


def _configured_secrets(settings: Settings) -> set[str]:
    secrets: set[str] = set()
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if value is None:
            continue
        if name.endswith(("_key", "_token", "_secret", "_password")):
            raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
            if raw:
                secrets.add(raw)
        if isinstance(value, str) and "://" in value:
            try:
                url = urlsplit(value)
                if url.password:
                    secrets.add(unquote(url.password))
                for key, item in parse_qsl(url.query):
                    if item and _SECRET_NAME.fullmatch(key):
                        secrets.add(item)
            except ValueError:
                # Malformed endpoint diagnostics still pass through the text rules.
                pass
    # Short/default values are common prose; redact them only in credential contexts.
    return {raw for raw in secrets
            if len(raw) >= 8 and raw.strip().casefold() not in _PLACEHOLDER_SECRETS}


def redact_error(
    error: str | BaseException | None,
    *,
    settings: Settings | None = None,
    max_length: int | None = ERROR_MAX_LENGTH,
    include_type: bool = False,
) -> str | None:
    """Remove credentials before truncation, preserving the original message by default.

    Explicit service settings supplement the process settings (injected clients may
    have a different configuration). Never cache secret values independently of them.
    Opt into exception types only for unexpected failures stored as diagnostics.
    Archived evaluation contracts can disable truncation with max_length=None.
    """
    if error is None:
        return None
    text = (f"{type(error).__name__}: {error}"
            if include_type and isinstance(error, BaseException) else str(error))
    text = _URL_USERINFO.sub(r"\1" + REDACTED + "@", text)
    text = _QUERY.sub(
        lambda match: match[1] + match[2] + "=" + REDACTED
        if _SECRET_NAME.fullmatch(unquote(match[2])) else match[0],
        text,
    )
    text = _AUTH.sub(r"\1" + REDACTED, text)
    text = _BEARER.sub(r"\1" + REDACTED, text)
    text = _ASSIGNMENT.sub(lambda match: match["label"] + match["sep"] + REDACTED, text)
    text = _SK_KEY.sub(REDACTED, text)
    secrets = _configured_secrets(get_settings())
    if settings is not None:
        secrets.update(_configured_secrets(settings))
    variants = {variant for secret in secrets for variant in (secret, quote(secret, safe=""),
                                                             quote_plus(secret))}
    if variants:
        # One pass prevents overlapping secret values from revealing a suffix.
        text = re.sub(
            "|".join(re.escape(value) for value in sorted(variants, key=len, reverse=True)),
            lambda _: REDACTED, text,
        )
    if max_length is not None and len(text) > max_length:
        return text[:max(0, max_length - 3)] + "." * min(3, max_length)
    return text


def redact_error_payload(value: Any) -> Any:
    """Sanitize structured error details without changing machine-readable codes."""
    if isinstance(value, dict):
        return {key: item if key == "code" else redact_error_payload(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_error_payload(item) for item in value]
    return redact_error(value) if isinstance(value, (str, BaseException)) else value
