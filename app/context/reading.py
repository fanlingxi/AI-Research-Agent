"""Versioned reading payloads derived from immutable snapshots, with inline evidence."""

from __future__ import annotations

from typing import Literal

ReadingFormat = Literal["legacy", "research-v1", "inline-v1"]


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
    if package.reading_format == "inline-v1":
        result["knowledge"] = knowledge_reading_payload(package.knowledge, package.reading_format)
    return result


def knowledge_reading_payload(value, reading_format):
    """Serialize knowledge or a single bundle using the Runtime reading format."""
    if reading_format == "legacy":
        return value.model_dump(mode="json", exclude_defaults=True, exclude_none=True)
    result = value.model_dump(mode="json")
    if reading_format == "inline-v1":

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
    return result
