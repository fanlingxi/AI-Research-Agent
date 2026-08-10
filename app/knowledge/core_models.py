from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceRecord(BaseModel):
    id: str
    source_type: str
    title: str = ""
    uri: str
    canonical_uri: str = ""
    version: str = ""
    content_sha256: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class ChunkRecord(BaseModel):
    id: str
    document_id: str
    legacy_chunk_id: str | None = None
    content: str
    content_sha256: str
    chunk_index: int
    page_start: int
    page_end: int
    location: dict[str, Any] = Field(default_factory=dict)
    embedding_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class CoreEntity(BaseModel):
    id: str
    legacy_id: str | None = None
    name: str
    normalized_name: str
    entity_type: str
    domain: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: str
    updated_at: str


class CoreRelation(BaseModel):
    id: str
    legacy_id: str | None = None
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    domain: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: str
    updated_at: str


class CoreClaim(BaseModel):
    id: str
    legacy_id: str | None = None
    entity_id: str | None = None
    relation_id: str | None = None
    subject: str = ""
    predicate: str = ""
    object_value: str = ""
    claim_type: str
    statement: str
    confidence: float | None = None
    status: str
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class CoreEntityClaimOverride(BaseModel):
    """A typed claim representation for a reviewed domain Entity.

    The Core remains generic: it only persists an Entity, Claim, and Evidence
    chain. A reviewed Domain Plugin may supply this representation while the
    existing published-entity compatibility record and Collection membership
    remain the authoritative scope bridge.
    """

    subject: str
    predicate: str
    object_value: str
    claim_type: str
    statement: str
    confidence: float | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class CoreEvidence(BaseModel):
    id: str
    source_id: str | None = None
    chunk_id: str | None = None
    quote: str
    quote_sha256: str
    location: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class BackfillSummary(BaseModel):
    version: int
    status: str
    sources: int = 0
    documents: int = 0
    chunks: int = 0
    entities: int = 0
    relations: int = 0
    claims: int = 0
    evidences: int = 0
    evidence_links: int = 0
    needs_attention: int = 0
    failed: int = 0
