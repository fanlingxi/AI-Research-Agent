from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceSourceTier = Literal["primary_fulltext", "unknown"]


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


class TextSpan(BaseModel):
    """Half-open offsets into one unchanged ParsedDocument.page_texts entry."""

    page: int
    start: int
    end: int


class DocumentBlock(BaseModel):
    kind: Literal[
        "title", "front_matter", "abstract", "keywords", "heading", "paragraph",
        "header", "footer", "references", "caption", "other",
    ]
    text: str
    spans: list[TextSpan]


class ParsedDocument(BaseModel):
    """Text extracted from a PDF or document source."""

    source: str
    title: str | None = None
    text: str
    pages: int = 0
    page_texts: list[str] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    blocks: list[DocumentBlock] | None = None
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
