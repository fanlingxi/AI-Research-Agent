from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

KnowledgeNodeType = Literal[
    "Paper",
    "Topic",
    "Concept",
    "Method",
    "Task",
    "Dataset",
    "Metric",
    "Finding",
]
KnowledgeRelationType = Literal[
    "PRESENTS",
    "ADDRESSES",
    "USES",
    "EVALUATES",
    "IMPROVES",
    "COMPARES_WITH",
    "APPLIES_TO",
    "HAS_LIMITATION",
    "SUPPORTS",
]
CandidateStatus = Literal["draft", "approved", "rejected", "merged", "published"]
IngestionStatus = Literal[
    "queued",
    "running",
    "needs_review",
    "publishing",
    "completed",
    "failed",
    "interrupted",
]
JobStatus = Literal["queued", "running", "completed", "failed"]
ProjectionStatus = Literal["queued", "running", "completed", "failed", "not_required"]
ReportStatus = Literal["queued", "running", "completed", "failed"]


class EvidenceSpan(BaseModel):
    """A human-reviewable pointer from a knowledge assertion to source text."""

    paper_id: str
    chunk_id: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=600)

    @field_validator("page_end")
    @classmethod
    def end_after_start(cls, value: int, info) -> int:
        start = info.data.get("page_start", value)
        if value < start:
            raise ValueError("page_end cannot be earlier than page_start")
        return value


class ExtractedEntity(BaseModel):
    """One LLM-proposed node before a reviewer makes it formal knowledge."""

    name: str = Field(min_length=2, max_length=160)
    type: KnowledgeNodeType
    summary: str = Field(min_length=12, max_length=900)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: EvidenceSpan


class ExtractedRelation(BaseModel):
    """One LLM-proposed semantic relation using display names as endpoints."""

    source_name: str = Field(min_length=2, max_length=160)
    target_name: str = Field(min_length=2, max_length=160)
    type: KnowledgeRelationType
    summary: str = Field(min_length=12, max_length=900)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: EvidenceSpan


class PaperReading(BaseModel):
    """Reading-card content produced with the same evidence constraints as entities."""

    research_problem: str = Field(min_length=12, max_length=1200)
    core_contributions: list[str] = Field(min_length=1, max_length=6)
    method_summary: str = Field(min_length=12, max_length=1200)
    key_results: list[str] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=6)
    evidence: EvidenceSpan


class KnowledgeExtraction(BaseModel):
    """Schema-constrained response expected from the configured live LLM."""

    reading: PaperReading
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=20)
    relations: list[ExtractedRelation] = Field(default_factory=list, max_length=30)


class CandidateEntity(BaseModel):
    id: str
    ingestion_id: str
    topic_slug: str
    name: str
    type: KnowledgeNodeType
    summary: str
    aliases: list[str] = Field(default_factory=list)
    confidence: float
    evidence: EvidenceSpan
    status: CandidateStatus = "draft"
    canonical_id: str | None = None
    merge_suggestions: list[MergeSuggestion] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateRelation(BaseModel):
    id: str
    ingestion_id: str
    topic_slug: str
    source_candidate_id: str
    target_candidate_id: str
    type: KnowledgeRelationType
    summary: str
    confidence: float
    evidence: EvidenceSpan
    status: CandidateStatus = "draft"
    metadata: dict[str, Any] = Field(default_factory=dict)


class MergeSuggestion(BaseModel):
    """A possible canonical entity; the reviewer still makes the merge decision."""

    entity_id: str
    name: str
    type: KnowledgeNodeType
    similarity: float = Field(ge=0.0, le=1.0)
    match_kind: Literal["exact", "similar"]


class PublishedEntity(BaseModel):
    id: str
    name: str
    type: KnowledgeNodeType
    summary: str
    aliases: list[str] = Field(default_factory=list)
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    topic_slugs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PublishedRelation(BaseModel):
    id: str
    source_entity_id: str
    target_entity_id: str
    type: KnowledgeRelationType
    summary: str
    confidence: float
    evidence: list[EvidenceSpan] = Field(default_factory=list)
    topic_slugs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestion(BaseModel):
    id: str
    topic: str
    topic_slug: str
    sources: list[str]
    pdf_max_pages: int
    status: IngestionStatus
    created_at: str
    updated_at: str
    error: str | None = None
    document_count: int = 0
    candidate_count: int = 0
    published_count: int = 0
    queued_job_count: int = 0
    pending_projection_count: int = 0
    failed_projection_count: int = 0
    vault_path: str | None = None


class CandidateDecision(BaseModel):
    decision: Literal["approve", "reject", "merge"]
    canonical_id: str | None = None

    @field_validator("canonical_id")
    @classmethod
    def merge_requires_canonical(cls, value: str | None, info) -> str | None:
        if info.data.get("decision") == "merge" and not value:
            raise ValueError("canonical_id is required when merging")
        return value


class DecisionResult(BaseModel):
    candidate: dict[str, Any]
    applied: bool
    replayed: bool
    projection_status: ProjectionStatus


class BulkApprovalResult(BaseModel):
    ingestion: KnowledgeIngestion
    published_entities: int = 0
    published_relations: int = 0
    skipped_conflicts: int = 0
    blocked_relations: int = 0


class KnowledgeJob(BaseModel):
    id: str
    kind: Literal["ingestion", "report"]
    resource_id: str
    status: JobStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    lease_until: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class ProjectionEvent(BaseModel):
    id: str
    ingestion_id: str
    candidate_id: str
    aggregate_type: Literal["entity", "relation"]
    aggregate_id: str
    topic_slug: str
    status: ProjectionStatus
    attempts: int = 0
    lease_until: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class ReportEvidence(BaseModel):
    id: str
    paper_id: str
    chunk_id: str
    title: str
    text: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    score: float = 0.0


class ReportEvaluation(BaseModel):
    evidence_grounding: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    citation_fidelity: float = Field(default=0.0, ge=0.0, le=1.0)
    structure_score: float = Field(default=0.0, ge=0.0, le=1.0)
    cited_evidence: list[str] = Field(default_factory=list)
    invalid_citations: list[str] = Field(default_factory=list)
    revision_applied: bool = False
    passed: bool = False


class ResearchReport(BaseModel):
    id: str
    query: str
    topic_slugs: list[str] = Field(default_factory=list)
    top_k: int = 8
    report_depth: Literal["brief", "standard", "deep"] = "standard"
    status: ReportStatus
    content: str = ""
    evidence: list[ReportEvidence] = Field(default_factory=list)
    evaluation: ReportEvaluation | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str
    updated_at: str
