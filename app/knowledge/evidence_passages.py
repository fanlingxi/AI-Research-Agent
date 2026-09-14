"""Lossless, bounded passage ranges; sentence boundaries are a heuristic, not proof."""

from __future__ import annotations

import re

PASSAGE_VERSION = "sentence-2400-v2"
_BOUNDARY = re.compile(r'[.!?。！？]["”’\)\]]*(?:\s+|$)|\n[ \t]*\n')


def passage_ranges(text: str):
    """Prefer the first boundary after 2400 chars; explicitly flag hard cuts.

    Never normalize or drop whitespace: callers can preserve original offsets.
    An overlong sentence cannot exceed the 6000-character evidence contract.
    """
    start = 0
    while start < len(text):
        limit = min(start + 6000, len(text))
        target = min(start + 2400, len(text))
        match = None
        # Do not pass endpos: regex '$' at the cap would mistake a decimal
        # point at character 6000 for an actual end-of-text boundary.
        for candidate in _BOUNDARY.finditer(text, start):
            if candidate.end() > limit:
                break
            if candidate.end() >= target:
                match = candidate
                break
        end = match.end() if match else limit
        yield start, end, match is None and end < len(text)
        start = end
