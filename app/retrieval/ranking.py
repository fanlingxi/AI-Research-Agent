"""Shared deterministic operations for SQLite-verified retrieval candidates.

Callers retain authorization, evidence completeness, scoring and final budget
policy. These pure functions neither fetch candidates nor establish trust in
them; in particular, sorting projection IDs does not make them evidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TypeVar

T = TypeVar("T")


def query_terms(query: str) -> list[str]:
    """Preserve the legacy bilingual terms, insertion order and twelve-term cap."""

    terms = re.findall(r"[a-zA-Z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", query.casefold())
    return list(dict.fromkeys(terms))[:12]


def term_coverage(terms: list[str], text: str) -> float:
    """Return legacy substring coverage, not a tokenizer or semantic score."""

    if not terms:
        return 0.0
    normalized = text.casefold()
    return sum(term in normalized for term in terms) / len(terms)


def rank_scored_candidates(
    candidates: Iterable[T],
    *,
    score: Callable[[T], float],
    identity: Callable[[T], tuple[str, ...]],
    group: Callable[[T], str] | None = None,
    max_per_group: int | None = None,
) -> list[T]:
    """Order by descending score then identity, optionally capping each group.

    Scores remain in the caller's scale and are never rounded, normalized or
    fused here. Stable ties and duplicate handling remain the caller's legacy
    behavior. Group capping happens before the caller's final count/token cap.
    """

    if (group is None) != (max_per_group is None):
        raise ValueError("group and max_per_group must be supplied together")
    if max_per_group is not None and max_per_group < 1:
        raise ValueError("max_per_group must be positive")
    ranked = sorted(candidates, key=lambda item: (-score(item), identity(item)))
    if group is None:
        return ranked
    selected: list[T] = []
    counts: dict[str, int] = {}
    for item in ranked:
        group_id = group(item)
        if counts.get(group_id, 0) >= max_per_group:
            continue
        selected.append(item)
        counts[group_id] = counts.get(group_id, 0) + 1
    return selected
