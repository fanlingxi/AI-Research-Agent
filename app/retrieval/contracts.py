"""Non-authoritative candidates and SQLite-bound retrieval audit records."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CandidateTarget = Literal["chunk", "entity", "relation", "legacy_entity", "legacy_relation"]


class SourceIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    source_sha256: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_sha256: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    chunk_sha256: str = Field(min_length=1)


@dataclass(frozen=True)
class CandidateReference:
    channel: Literal["vector", "graph"]
    target_type: CandidateTarget
    target_id: str
    score: float = 0.5
    source_identity: SourceIdentity | None = None


class CandidateMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: Literal["chunk", "claim", "relation"]
    item_id: str
    sources: list[SourceIdentity] = Field(min_length=1)


class CandidateAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    channel_rank: int = Field(ge=1)
    target_type: str
    target_id: str
    raw_score: float | None = Field(default=None, allow_inf_nan=False)
    supplied_source: SourceIdentity | None = None
    status: Literal["verified", "rejected"]
    reason: str
    matches: list[CandidateMatch] = Field(default_factory=list)


class SelectionAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_type: Literal["chunk", "claim", "relation"]
    item_id: str
    selected: bool
    reason: str
    rank: int | None = Field(default=None, ge=1)
    score: float | None = Field(default=None, allow_inf_nan=False)
    sources: list[SourceIdentity] = Field(min_length=1)


class RetrievalAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    strategy_id: str
    parameters: dict[str, Any]
    scope: dict[str, Any]
    candidates: list[CandidateAudit] = Field(default_factory=list)
    selections: list[SelectionAudit] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)


def validate_candidates(
    references: list[CandidateReference],
    bindings: Mapping[tuple[str, str], list[CandidateMatch]],
) -> list[CandidateAudit]:
    """Bind IDs only to caller-verified SQLite objects; retain every raw position.

    Bindings must already satisfy the path's authority/completeness policy.
    Missing provider versions remain unknown: binding current SQLite evidence
    is not proof that the projection score was computed from that version.
    """

    counts: dict[str, int] = {}
    records: list[CandidateAudit] = []
    for reference in references:
        channel = str(reference.channel)
        counts[channel] = counts.get(channel, 0) + 1
        valid_score = isinstance(reference.score, (int, float)) and math.isfinite(reference.score)
        supplied = reference.source_identity
        valid = (
            channel in {"vector", "graph"}
            and isinstance(reference.target_type, str)
            and reference.target_type
            in {"chunk", "entity", "relation", "legacy_entity", "legacy_relation"}
            and isinstance(reference.target_id, str)
            and bool(reference.target_id)
            and valid_score
            and (supplied is None or isinstance(supplied, SourceIdentity))
        )
        matches = (
            list(bindings.get((reference.target_type, reference.target_id), [])) if valid else []
        )
        reason = "sqlite_bound_provider_version_unknown"
        if not valid:
            reason = "invalid_candidate"
        elif not matches:
            reason = "not_in_verified_scope"
        elif supplied is not None:
            matches = [match for match in matches if supplied in match.sources]
            reason = "sqlite_bound_version_matched" if matches else "source_version_mismatch"
        records.append(
            CandidateAudit(
                channel=channel,
                channel_rank=counts[channel],
                target_type=str(reference.target_type),
                target_id=str(reference.target_id),
                raw_score=float(reference.score) if valid_score else None,
                supplied_source=supplied if isinstance(supplied, SourceIdentity) else None,
                status="verified" if matches else "rejected",
                reason=reason,
                matches=matches,
            )
        )
    return records
