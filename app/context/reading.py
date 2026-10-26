"""Versioned reading payloads derived from immutable snapshots, with inline evidence."""

from __future__ import annotations

from typing import Literal

from app.knowledge.core_repository import _normalize_for_match

ReadingFormat = Literal["legacy", "research-v1", "inline-v1", "compact-v1"]

COMPACT_READING_NOTES = (
    "Bundle document_ids/source_ids/chunk_ids reference knowledge.documents/sources/chunks; "
    "same_as_evidence_quote references an evidence_id in the same bundle "
    "and means its exact quote; "
    "quote_normalized_derived marks an omitted locator.quote_normalized equal to the quote "
    "with outer whitespace stripped, whitespace collapsed and casefolded."
)


def research_payload(package):
    result = {
        "context_snapshot_id": package.snapshot_id,
        # Keep pre-freeze token estimation stable before the digest is computed.
        "context_sha256": package.package_sha256 or "0" * 64,
        **{
            name: getattr(package, name).model_dump(mode="json")
            for name in ("project", "task", "memory", "knowledge", "artifacts", "constraints")
        },
    }
    if package.reading_format in {"inline-v1", "compact-v1"}:
        result["knowledge"] = knowledge_reading_payload(package.knowledge, package.reading_format)
    if package.reading_format == "compact-v1":
        result["reading_notes"] = COMPACT_READING_NOTES
    return result


def knowledge_reading_payload(value, reading_format):
    """Serialize knowledge or a single bundle using the Runtime reading format."""
    if reading_format == "legacy":
        return value.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    result = value.model_dump(mode="json")
    if reading_format in {"inline-v1", "compact-v1"}:

        def strip(value):
            if isinstance(value, dict):
                return {
                    # Free-form provenance is data, even when its keys resemble
                    # administrative model fields. Preserve it without filtering.
                    k: v if k in {"location", "metadata"} else strip(v)
                    for k, v in value.items()
                    if k not in {"selection", "created_at", "updated_at", "legacy_id"}
                }
            if isinstance(value, list):
                return [strip(v) for v in value]
            return value

        result = strip(result)
    if reading_format == "compact-v1":
        # A single bundle is costed as a one-bundle compact view with its own tables.
        result = result if "claim_bundles" in result else {"claim_bundles": [result]}
        _compact_knowledge(result)
    return result


def _compact_knowledge(knowledge):
    """Deduplicate only the detached reading view; never mutate a stored package.

    Conflicting IDs fail explicitly rather than making provenance ambiguous.
    Equality is checked after inline administrative fields have been stripped.
    """
    tables = {"documents": "document_id", "sources": "source_id", "chunks": "chunk_id"}
    for name in tables:
        knowledge[name] = {}
    for bundle in knowledge["claim_bundles"]:
        for name, id_field in tables.items():
            references = []
            for item in bundle.pop(name):
                item_id = item[id_field]
                table = knowledge[name]
                if item_id in table and table[item_id] != item:
                    raise ValueError(f"compact-v1: conflicting {id_field} {item_id!r}")
                table[item_id] = item
                references.append(item_id)
            bundle[f"{id_field}s"] = references

        quotes_by_id = {}
        for evidence in bundle["evidence"]:
            quotes_by_id.setdefault(evidence["evidence_id"], set()).add(evidence["quote"])
            locator = evidence["location"].get("locator")
            if isinstance(locator, dict) and locator.get("quote_normalized") == (
                _normalize_for_match(evidence["quote"].strip())
            ):
                del locator["quote_normalized"]
                # Distinguish an omitted derived value from one absent in the snapshot.
                evidence["quote_normalized_derived"] = True
        quote_ids = {
            next(iter(quotes)): evidence_id
            for evidence_id, quotes in quotes_by_id.items()
            if len(quotes) == 1
        }
        for field in ("statement", "object_value"):
            value = bundle["claim"][field]
            if value in quote_ids:
                bundle["claim"][field] = {"same_as_evidence_quote": quote_ids[value]}
