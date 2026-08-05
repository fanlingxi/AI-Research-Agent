from __future__ import annotations

import re
from typing import Any

_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def sanitize_utf8_text(value: str) -> str:
    """Replace lone Unicode surrogates so text can cross UTF-8 JSON boundaries.

    PDF text extractors may expose malformed characters from embedded font maps.
    Replacing only those invalid code points preserves all valid text while making
    the value safe for embedding APIs and vector-store payloads.
    """

    return _LONE_SURROGATE.sub("�", value)


def sanitize_json_value(value: Any) -> Any:
    """Recursively make a JSON-compatible payload safe to UTF-8 encode."""

    if isinstance(value, str):
        return sanitize_utf8_text(value)
    if isinstance(value, list):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {
            sanitize_utf8_text(key) if isinstance(key, str) else key: sanitize_json_value(item)
            for key, item in value.items()
        }
    return value
