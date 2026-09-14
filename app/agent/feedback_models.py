"""Human feedback is an audit record, never formal knowledge or a judge truth."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import MAX_RUN_TOKENS


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=128)
    category: Literal[
        "citation", "unsupported", "incomplete", "scope", "source_version", "execution", "other"
    ]
    note: str = Field(min_length=1, max_length=3000)
    reporter: str = Field(min_length=1, max_length=200)
    # One-based structured finding, or the run itself for failures with no output.
    finding_index: int | None = Field(default=None, ge=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class FeedbackDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    decision: Literal["accepted", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=3000)


class FeedbackRerunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    # Documents the intervening task correction or reviewed evidence change.
    resolution_note: str = Field(min_length=1, max_length=3000)
    # Explicit override for the NEW run; omission preserves the original limit.
    token_budget: int | None = Field(default=None, ge=256, le=MAX_RUN_TOKENS)
    context_max_tokens: int | None = Field(default=None, ge=256, le=262144)


class FeedbackRecheck(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: Literal["resolved", "unresolved"]
    reviewer: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=3000)


class FeedbackExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # A candidate for a NEW dataset version; it does not mutate any dataset.
    target_dev_version: str = Field(min_length=1, max_length=128)
