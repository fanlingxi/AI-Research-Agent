from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class KnowledgeIngestionRequest(BaseModel):
    topic: str | None = Field(default=None, min_length=2, max_length=500)
    collection: str | None = Field(default=None, min_length=2, max_length=500)
    sources: list[str] = Field(min_length=1, max_length=30)
    pdf_max_pages: int = Field(default=20, ge=1, le=150)

    @model_validator(mode="after")
    def collection_aliases_must_agree(self):
        if self.topic and self.collection and self.topic.strip() != self.collection.strip():
            raise ValueError("topic 与 collection 同时提供时必须一致。")
        return self


class CollectionRequest(BaseModel):
    collection: str = Field(min_length=2, max_length=500)


class KnowledgeCandidatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    summary: str | None = Field(default=None, min_length=12, max_length=900)
    aliases: list[str] | None = Field(default=None, max_length=12)
    sense_qualifier: str | None = Field(default=None, max_length=160)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class GameKnowledgeReviewRequest(BaseModel):
    review_note: str | None = Field(default=None, max_length=1200)


class ReportCreateRequest(BaseModel):
    query: str = Field(min_length=3, max_length=1000)
    topic_slugs: list[str] = Field(default_factory=list, max_length=20)
    collection_slugs: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=8, ge=1, le=30)
    report_depth: Literal["brief", "standard", "deep"] = "standard"

    @model_validator(mode="after")
    def collection_scope_aliases_must_agree(self):
        if self.topic_slugs and self.collection_slugs and self.topic_slugs != self.collection_slugs:
            raise ValueError("topic_slugs 与 collection_slugs 同时提供时必须一致。")
        return self
