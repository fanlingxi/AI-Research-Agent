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
    # Domain-owned labels remain opaque strings to the Core. They let the
    # existing candidate/review/published pipeline carry reviewed Game facts
    # without creating a second Knowledge store.
    "Formula",
    "Patch",
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
CandidateStatus = Literal["draft", "approved", "rejected", "merged", "published", "deferred"]
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
    sense_qualifier: str = Field(default="", max_length=160)
    paper_context: str = Field(default="", max_length=1200)
    role: str = Field(default="", max_length=900)
    conditions: str = Field(default="", max_length=900)
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
    sense_qualifier: str = ""
    paper_context: str = ""
    role: str = ""
    conditions: str = ""
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
    collection_slugs: list[str] = Field(default_factory=list)
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
    collection_slugs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestion(BaseModel):
    id: str
    topic: str
    topic_slug: str
    collection: str = "收件箱"
    collection_slug: str = "inbox"
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
    job_status: JobStatus | None = None
    job_attempts: int = 0
    queue_position: int | None = None
    pending_projection_count: int = 0
    failed_projection_count: int = 0
    vault_path: str | None = None
    deduplicated: bool = False


class CandidateDecision(BaseModel):
    decision: Literal["approve", "reject", "merge", "link", "defer"]
    canonical_id: str | None = None
    review_note: str | None = Field(default=None, max_length=1200)

    @field_validator("canonical_id")
    @classmethod
    def merge_requires_canonical(cls, value: str | None, info) -> str | None:
        if info.data.get("decision") in {"merge", "link"} and not value:
            raise ValueError("canonical_id is required when linking or merging")
        return value


class BulkCandidateDecision(BaseModel):
    """One explicit review action applied to a bounded set of candidates."""

    candidate_ids: list[str] = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject", "defer"]
    review_note: str | None = Field(default=None, max_length=1200)

    @field_validator("candidate_ids")
    @classmethod
    def candidate_ids_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("candidate_ids must not contain duplicates")
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


class BulkCandidateDecisionIssue(BaseModel):
    candidate_id: str
    reason: str


class BulkCandidateDecisionResult(BaseModel):
    ingestion: KnowledgeIngestion
    decision: Literal["approve", "reject", "defer"]
    requested: int
    applied: int = 0
    replayed: int = 0
    skipped: list[BulkCandidateDecisionIssue] = Field(default_factory=list)


class ConfidenceAutoApprovalResult(BulkCandidateDecisionResult):
    """A deterministic automatic approval run using a strict confidence cutoff."""

    min_confidence_exclusive: float


class KnowledgeJob(BaseModel):
    id: str
    kind: Literal[
        "ingestion", "report", "collection_sync", "agent_run", "research_command"
    ]
    resource_id: str
    status: JobStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    attempts: int = 0
    priority: int = 0
    lease_until: str | None = None
    lease_owner: str | None = None
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
    lease_owner: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class ExecutorHeartbeat(BaseModel):
    id: str
    role: str
    version: str
    started_at: str
    last_heartbeat_at: str
    current_job_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeCollection(BaseModel):
    slug: str
    name: str
    is_system: bool = False
    ingestion_count: int = 0
    updated_at: str | None = None


class SourceMention(BaseModel):
    """A reviewed, paper-scoped use of a term before cross-paper identity is decided."""

    id: str
    candidate_id: str
    ingestion_id: str
    paper_id: str
    name: str
    type: KnowledgeNodeType
    summary: str
    sense_qualifier: str = ""
    paper_context: str = ""
    role: str = ""
    conditions: str = ""
    aliases: list[str] = Field(default_factory=list)
    evidence: EvidenceSpan
    status: Literal["source_only", "linked", "rejected"]
    created_at: str
    updated_at: str


class ConceptSense(BaseModel):
    """A canonical, reviewer-approved meaning that may have many paper mentions."""

    id: str
    name: str
    type: KnowledgeNodeType
    qualifier: str = ""
    definition: str
    scope: str = ""
    aliases: list[str] = Field(default_factory=list)
    status: Literal["published", "legacy"] = "published"
    source_mention_ids: list[str] = Field(default_factory=list)


class ReportEvidence(BaseModel):
    id: str
    paper_id: str
    chunk_id: str
    title: str
    text: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    score: float = 0.0


class ChunkSearchHit(BaseModel):
    """Untrusted vector candidate; SQLite must rehydrate every display field."""

    chunk_id: str
    score: float = 0.0


class ReportEvaluation(BaseModel):
    evidence_grounding: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    citation_fidelity: float = Field(default=0.0, ge=0.0, le=1.0)
    structure_score: float = Field(default=0.0, ge=0.0, le=1.0)
    retrieval_relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    source_diversity: float = Field(default=1.0, ge=0.0, le=1.0)
    selected_source_count: int = 0
    available_relevant_source_count: int = 0
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
