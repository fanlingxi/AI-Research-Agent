"""Locate model-copied quotes in source text without accepting paraphrase.

PDF extraction and model copying disagree on layout noise: line breaks, spacing between
CJK characters, full/half width forms, curly quotes and dash variants. Matching ignores
only that noise and returns the exact original span, so verification still proves the
words came from the source in order. Ellipses, reordering and rewording still fail.
"""

from __future__ import annotations

import unicodedata

_TRANSLATE = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "′": "'",
        "“": '"', "”": '"', "„": '"', "″": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
        "―": "-", "−": "-",
    }
)
_IGNORED = {"­", "​", "‌", "‍", "﻿"}


def _fold(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        if char in _IGNORED or char.isspace():
            continue
        for folded in unicodedata.normalize("NFKC", char).casefold().translate(_TRANSLATE):
            if not folded.isspace():
                chars.append(folded)
                origin.append(index)
    return "".join(chars), origin


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Return the [start, end) span of ``text`` that matches ``quote`` modulo layout noise."""
    needle, _ = _fold(quote)
    if not needle:
        return None
    haystack, origin = _fold(text)
    start = haystack.find(needle)
    if start < 0:
        return None
    return origin[start], origin[start + len(needle) - 1] + 1


def contains_quote(quote: str, text: str) -> bool:
    return locate_quote(quote, text) is not None
