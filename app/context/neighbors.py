"""Bounded, non-recursive adjacency over complete SQLite-authorized bundles."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Literal

from app.context.knowledge_reader import RawKnowledgeBundle
from app.context.ranking import RankedKnowledgeBundle

ContextNeighborMode = Literal["off", "adjacent-v1"]
MAX_ANCHORS = 10


def _coordinate(bundle: RawKnowledgeBundle):
    if not bundle.evidence or len({e.chunk["id"] for e in bundle.evidence}) != 1:
        return None
    first = bundle.evidence[0]
    if first.source_span is None or any(e.source_span != first.source_span
                                       for e in bundle.evidence):
        return None
    identity = (first.source["id"], first.source["version"], first.source["content_sha256"],
                first.document["id"], first.document["content_sha256"],
                first.document["parser_version"])
    return identity, *first.source_span


def adjacent_bundles(
    bundles: list[RawKnowledgeBundle], ranked: list[RankedKnowledgeBundle],
) -> tuple[list[RankedKnowledgeBundle], list[dict]]:
    """Insert one touching predecessor/successor for each of the first ten seeds.

    No raw document text is fetched here. Neighbors must themselves be complete
    authorized bundles with verified offsets. Groups partition emitted claims;
    references to earlier groups remain available whenever this prefix is kept.
    """
    raw = {b.claim["id"]: b for b in bundles}
    ranks = {r.bundle.claim["id"]: r for r in ranked}
    coordinates = {key: c for key, b in raw.items() if (c := _coordinate(b)) is not None}
    starts, ends = defaultdict(list), defaultdict(list)
    for key, (identity, start, end) in coordinates.items():
        starts[identity, start].append(key)
        ends[identity, end].append(key)

    def order(key):
        return ranks[key].rank if key in ranks else float("inf"), key

    selected, emitted, groups = set(), [], []
    for index, anchor in enumerate(ranked):
        anchor_id = anchor.bundle.claim["id"]
        if anchor_id in selected:
            continue
        context_ids = [anchor_id]
        coordinate = coordinates.get(anchor_id)
        if index < MAX_ANCHORS and coordinate is not None:
            identity, start, end = coordinate
            for candidates in (ends.get((identity, start), []), starts.get((identity, end), [])):
                if candidates:
                    context_ids.append(min(candidates, key=order))
            context_ids.sort(key=lambda key: (coordinates[key][1], key))
        added = [key for key in context_ids if key not in selected]
        for key in added:
            existing = ranks.get(key)
            if existing is not None:
                item = replace(existing, rank=len(emitted) + 1)
            else:
                item = RankedKnowledgeBundle(
                    bundle=raw[key], rank=len(emitted) + 1, score=anchor.score,
                    score_breakdown={"anchor_score": anchor.score}, channels=["adjacent"],
                    selected_reason="Independently verified adjacent claim bundle",
                )
            emitted.append(item)
            selected.add(key)
        groups.append({
            "anchor_claim_id": anchor_id, "anchor_original_rank": anchor.rank,
            "claim_ids": added, "context_claim_ids": context_ids,
            "members": [{"claim_id": key, "original_rank": ranks[key].rank if key in ranks
                         else None, "coordinate": coordinates.get(key)} for key in added],
        })
    return emitted, groups
