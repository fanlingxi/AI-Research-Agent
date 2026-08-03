from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceSourceTier = Literal[
    "primary_fulltext",
    "online_metadata",
    "offline_demo",
    "unknown",
]


class PaperMetadata(BaseModel):
    """Normalized paper metadata from search providers."""

    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""
    year: int | None = None
    source: str = "unknown"
    url: str | None = None
    pdf_url: str | None = None
    published_at: str | None = None
    doi: str | None = None
    publication: str | None = None
    source_tier: EvidenceSourceTier = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedDocument(BaseModel):
    """Text extracted from a PDF or document source."""

    source: str
    title: str | None = None
    text: str
    pages: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentChunk(BaseModel):
    """A retrieval-ready text chunk."""

    id: str
    paper_id: str
    title: str
    text: str
    chunk_index: int
    token_count: int
    source_tier: EvidenceSourceTier = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalHit(BaseModel):
    """A ranked chunk returned by vector retrieval."""

    chunk_id: str
    paper_id: str
    title: str
    text: str
    score: float
    source_tier: EvidenceSourceTier = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
