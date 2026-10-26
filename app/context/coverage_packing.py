"""Deterministic first-fit packing of validated coverage anchors, without model calls."""

from __future__ import annotations

import json
import re
import unicodedata

from app.context.models import KnowledgeContext, runtime_context_payload
from app.retrieval.anchors import contains_quote


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def matched_sources(question_part, source_titles):
    """Conservative full-title matching; never infer aliases, pronouns or entities.

    Require an explicit comparison/separate-description cue in the validated span
    itself. Reject negation, shared titles and overlapping title mentions.
    """
    text = _normalized(question_part)
    if not re.search(
        r"\b(compare|comparison|contrast|versus|vs\.?|differences?|respectively|separately)\b"
        r"|比较|对比|区别|差异|分别说明|分别介绍|分别解释", text,
    ) or re.search(r"\b(no|not|without|don't)\b|不要|无需|不必|不比较|不对比", text):
        return []
    titles = {}
    for source_id, names in source_titles.items():
        for name in names:
            title = _normalized(name)
            if len(title) >= 4:
                titles.setdefault(title, set()).add(source_id)
    mentions = []
    for title, ids in sorted(titles.items()):
        # ASCII word boundaries avoid matching a short title inside another name.
        pattern = (r"(?<![a-z0-9_])" if title[0].isascii() and title[0].isalnum() else "")
        pattern += re.escape(title)
        pattern += r"(?![a-z0-9_])" if title[-1].isascii() and title[-1].isalnum() else ""
        for match in re.finditer(pattern, text):
            if len(ids) != 1:
                return []
            mentions.append((match.start(), match.end(), next(iter(ids))))
    mentions.sort()
    if any(left[1] > right[0] for left, right in zip(mentions, mentions[1:], strict=False)):
        return []
    sources = list(dict.fromkeys(item[2] for item in mentions))
    return sources if len(sources) >= 2 else []


class _ReadingCost:
    """Exact characters_div_4 marginal cost, serializing each bundle only once.

    The base is serialized once. Singleton reading views reuse the production
    formatter, including compact quote references and shared provenance tables.
    No growing full payload is serialized during first-fit.
    """

    def __init__(self, base):
        self.base = base
        payload = runtime_context_payload(base)
        self.empty_chars = len(_encoded(payload))
        knowledge = payload["knowledge"]
        skeleton = {**knowledge, "claim_bundles": []}
        self.chars = self.empty_chars - len(_encoded(knowledge)) + len(_encoded(skeleton))
        self.count = 0
        self.tables = {name: {} for name in ("documents", "sources", "chunks")
                       if name in knowledge}
        self.cache = {}

    def try_add(self, bundles, budget):
        chars, count = self.chars, self.count
        tables = {name: dict(table) for name, table in self.tables.items()}
        for bundle in bundles:
            key = bundle.claim.claim_id
            if key not in self.cache:
                single = self.base.model_copy(update={
                    "knowledge": KnowledgeContext(claim_bundles=[bundle]),
                })
                knowledge = runtime_context_payload(single)["knowledge"]
                self.cache[key] = (len(_encoded(knowledge["claim_bundles"][0])), {
                    name: {key: _encoded(value) for key, value in knowledge[name].items()}
                    for name in self.tables
                })
            size, additions = self.cache[key]
            chars += size + bool(count)
            count += 1
            for name, entries in additions.items():
                table = tables[name]
                for item_id, encoded in entries.items():
                    if item_id in table:
                        if table[item_id] != encoded:
                            raise ValueError(f"compact-v1: conflicting {name} {item_id!r}")
                    else:
                        chars += len(_encoded(item_id)) + 1 + len(encoded) + bool(table)
                        table[item_id] = encoded
        if (chars + 3) // 4 > budget:
            return False
        self.chars, self.count, self.tables = chars, count, tables
        return True


def pack_coverage(base, bundles, record, source_titles, neighbor_groups=None, pinned=()):
    """Pack whole groups: part coverage, explicit source coverage, extra anchors, fill.

    Group dependencies retain the context of earlier adjacency groups even when
    first-fit skips a prefix. Each candidate initiates at most one attempt.
    """
    by_id = {b.claim.claim_id: b for b in bundles}
    candidate_ids = record["candidate_claim_ids"]
    parts = record["parts"]
    anchors = [list(dict.fromkeys(candidate_ids[a["index"]] for a in p["anchors"]))
               for p in parts]
    anchor_sources = []
    for part in parts:
        sources = {}
        for anchor in part["anchors"]:
            key = candidate_ids[anchor["index"]]
            bundle = by_id.get(key)
            source_ids = sources.setdefault(key, set())
            if bundle:
                documents = {d.document_id: d.source_id for d in bundle.documents}
                source_ids.update(documents[c.document_id] for c in bundle.chunks
                                  if contains_quote(anchor["quote"], c.content))
        anchor_sources.append(sources)
    requirements = [matched_sources(p["question_part"], source_titles) for p in parts]
    groups = {key: set(g["context_claim_ids"]) | set(g["claim_ids"])
              for g in (neighbor_groups or []) for key in g["claim_ids"]}
    selected, reasons, skipped = set(), {}, set()
    ordered = []
    cost = _ReadingCost(base)

    def attempt(key):
        if key in selected:
            return True
        if key in reasons:
            return False
        unit, pending = set(), [key]
        while pending:
            member = pending.pop()
            if member in unit or member in selected:
                continue
            unit.add(member)
            pending.extend(groups.get(member, {member}) - unit)
        if not unit <= by_id.keys():
            reasons[key] = "group_conflict"
            return False
        additions = [b for b in bundles if b.claim.claim_id in unit]
        if not cost.try_add(additions, base.constraints.max_context_tokens):
            reasons[key] = "over_budget"
            skipped.update(unit)
            return False
        selected.update(unit)
        ordered.extend(additions)
        return True

    def covered_sources():
        return {source for mapping in anchor_sources for key, sources in mapping.items()
                if key in selected for source in sources}

    def prefer_sources(index, keys):
        covered = covered_sources()
        return sorted(keys, key=lambda key: not (anchor_sources[index][key] - covered))

    # User-pinned evidence (already capped to its budget share) goes first.
    for key in pinned:
        attempt(key)
    # First feasible anchor per part, in task order. Failed large anchors do not
    # prevent a smaller anchor from satisfying the same part.
    for index, keys in enumerate(anchors):
        if not selected.intersection(keys):
            for key in prefer_sources(index, keys):
                if attempt(key):
                    break
    # Explicitly named sources get priority over redundant secondary anchors.
    for index, required in enumerate(requirements):
        for source in required:
            keys = [key for key in anchors[index] if source in anchor_sources[index][key]]
            if not selected.intersection(keys):
                for key in keys:
                    if attempt(key):
                        break
    remaining = [[key for key in keys if key not in selected and key not in reasons]
                 for keys in anchors]
    while any(remaining):
        for index, keys in enumerate(remaining):
            if keys:
                key = prefer_sources(index, keys)[0]
                keys.remove(key)
                attempt(key)
    # Restore pre-rerank similarity order, including unanchored candidates.
    original = record["original_ranks"]
    for bundle in sorted(bundles, key=lambda b: original.get(b.claim.claim_id, float("inf"))):
        attempt(bundle.claim.claim_id)
    audit_parts = []
    for index, keys in enumerate(anchors):
        source_results = []
        for source in requirements[index]:
            matching = [key for key in keys if source in anchor_sources[index][key]]
            included = bool(selected.intersection(matching))
            source_results.append({
                "source_id": source, "selected": included,
                "reason": None if included else (
                    "not_anchored" if not matching else reasons.get(matching[0], "group_conflict")
                ),
            })
        audit_parts.append({
            "part_index": index,
            "selected": bool(selected.intersection(keys)),
            "reason": None if selected.intersection(keys) else (
                "not_anchored" if not keys else reasons.get(keys[0], "group_conflict")
            ),
            "anchors": [{"claim_id": key, "selected": key in selected,
                         "reason": None if key in selected else reasons.get(key, "group_conflict")}
                        for key in keys],
            "source_constraint_triggered": bool(requirements[index]),
            "matched_source_ids": requirements[index],
            "sources": source_results,
        })
    return ordered, {
        "strategy": "coverage-v3", "status": "packed", "parts": audit_parts,
        "first_fit_skipped_bundles": len(skipped - selected),
        "selected_bundles": len(selected),
        "measured_tokens": max(1, ((cost.chars if selected else cost.empty_chars) + 3) // 4),
    }
