"""Locate model-copied quotes in source text without accepting paraphrase.

PDF extraction and model copying disagree on layout noise: line breaks, spacing between
CJK characters, full/half width forms, curly quotes, dash variants and words hyphenated
at a line break ("re- trieval"). Matching ignores only that noise and returns the exact
original span, so verification still proves the words came from the source in order.
Ellipses, reordering and rewording still fail.
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


def _fold(text: str) -> tuple[str, list[int], set[int]]:
    """Fold layout noise; also return folded positions of line-break hyphens.

    A hyphen between a letter and whitespace followed by a lowercase letter is how PDF
    extraction leaves a word broken across lines. A quote may drop it ("retrieval") or
    keep it ("pseudo-documents"); numbers and capitalised continuations are untouched.
    """
    chars: list[str] = []
    origin: list[int] = []
    optional: set[int] = set()
    for index, char in enumerate(text):
        if char in _IGNORED or char.isspace():
            continue
        for folded in unicodedata.normalize("NFKC", char).casefold().translate(_TRANSLATE):
            if folded.isspace():
                continue
            if folded == "-" and _breaks_word(text, index):
                optional.add(len(chars))
            chars.append(folded)
            origin.append(index)
    return "".join(chars), origin, optional


def _breaks_word(text: str, index: int) -> bool:
    following = index + 1
    if index == 0 or not text[index - 1].isalpha():
        return False
    if following >= len(text) or not text[following].isspace():
        return False
    while following < len(text) and (text[following].isspace() or text[following] in _IGNORED):
        following += 1
    return following < len(text) and text[following].islower()


def _match_from(haystack: str, optional: set[int], needle: str, position: int) -> int | None:
    """Match greedily: an optional hyphen is consumed only when the quote has one too."""
    matched = 0
    while matched < len(needle):
        if position >= len(haystack):
            return None
        if haystack[position] == needle[matched]:
            matched += 1
        elif position not in optional:
            return None
        position += 1
    return position


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Return the [start, end) span of ``text`` that matches ``quote`` modulo layout noise."""
    needle, _, _ = _fold(quote)
    if not needle:
        return None
    haystack, origin, optional = _fold(text)
    start = haystack.find(needle)
    end = start + len(needle) if start >= 0 else None
    if end is None and optional:
        start = haystack.find(needle[0])
        while start >= 0 and end is None:
            end = _match_from(haystack, optional, needle, start)
            if end is None:
                start = haystack.find(needle[0], start + 1)
    if end is None:
        return None
    return origin[start], origin[end - 1] + 1


def contains_quote(quote: str, text: str) -> bool:
    return locate_quote(quote, text) is not None
