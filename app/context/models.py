"""Typed, serializable contracts for governed Context Packages."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.context.neighbors import ContextNeighborMode
from app.context.reading import ReadingFormat, research_payload
from app.retrieval.contracts import RetrievalAudit
from app.retrieval.query_planning import QueryPlanningMode


class ContextBuildRequest(BaseModel):
    """A bounded request to build context for one WorkspaceTask.

    ``project_id`` is optional for internal callers, but when supplied it must
    match the task's owning Project. The service derives authority from the
    persisted task rather than trusting a client-supplied scope.
    """

    task_id: str = Field(min_length=1)
    project_id: str | None = Field(default=None, min_length=1)
    snapshot_id: str | None = Field(default=None, min_length=1)
    max_tokens: int = Field(default=6000, ge=256, le=262144)
    enable_vector_candidates: bool = False
    enable_graph_candidates: bool = False
    retrieval_strategy: Literal["legacy", "bm25-v1", "hybrid-v1", "dense-v1"] = "legacy"
    query_planning: QueryPlanningMode = "off"
    context_neighbors: ContextNeighborMode = "off"
    evidence_reranking: Literal['off', 'llm-v1', 'coverage-v1', 'coverage-v2'] = 'off'
    reading_format: ReadingFormat = 'legacy'

    @model_validator(mode="after")
    def require_dense_channel(self):
        if self.retrieval_strategy == "dense-v1" and not self.enable_vector_candidates:
            raise ValueError("dense-v1 requires enable_vector_candidates")
        return self


class SelectionTrace(BaseModel):
    selected_reason: str
    retrieval_channels: list[str] = Field(default_factory=list)
    rank: int | None = Field(default=None, ge=1)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    estimated_tokens: int = Field(default=0, ge=0)
    provenance: dict[str, Any] = Field(default_factory=dict)


class TaskContext(BaseModel):
    task_id: str
    project_id: str
    title: str
    goal: str
    status: str
    priority: str
    expected_output: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str
    selection: SelectionTrace


class KnowledgeScopeContext(BaseModel):
    collection_slug: str
    created_at: str
    selection: SelectionTrace


class ProjectContext(BaseModel):
    project_id: str
    name: str
    goal: str
    domain: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str
    knowledge_scopes: list[KnowledgeScopeContext] = Field(default_factory=list)
    selection: SelectionTrace


class WorkspaceTaskContext(BaseModel):
    task_id: str
    project_id: str
    title: str
    goal: str
    status: str
    priority: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str
    selection: SelectionTrace


class DecisionContext(BaseModel):
    decision_id: str
    project_id: str
    task_id: str | None = None
    summary: str
    rationale: str
    impact: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str
    selection: SelectionTrace


class ArtifactReferenceContext(BaseModel):
    artifact_id: str
    project_id: str
    task_id: str | None = None
    type: str
    reference: str
    version: int
    status: str
    supersedes_artifact_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: int
    created_at: str
    updated_at: str
    selection: SelectionTrace


class MemoryContext(BaseModel):
    open_workspace_tasks: list[WorkspaceTaskContext] = Field(default_factory=list)
    accepted_decisions: list[DecisionContext] = Field(default_factory=list)
    required_constraints: list[str] = Field(default_factory=list)


class SourceContext(BaseModel):
    source_id: str
    source_type: str
    title: str
    uri: str
    canonical_uri: str
    version: str
    content_sha256: str
    created_at: str
    updated_at: str
    selection: SelectionTrace


class DocumentContext(BaseModel):
    document_id: str
    source_id: str
    title: str
    source: str
    pages: int
    content_sha256: str
    content_status: str
    parser_version: str
    parsed_at: str | None = None
    selection: SelectionTrace


class ChunkContext(BaseModel):
    chunk_id: str
    document_id: str
    chunk_index: int
    page_start: int
    page_end: int
    content: str
    content_sha256: str
    location: dict[str, Any] = Field(default_factory=dict)
    selection: SelectionTrace


class EvidenceContext(BaseModel):
    evidence_id: str
    claim_id: str
    source_id: str
    chunk_id: str
    quote: str
    quote_sha256: str
    location: dict[str, Any] = Field(default_factory=dict)
    selection: SelectionTrace


class EntityContext(BaseModel):
    entity_id: str
    legacy_id: str | None = None
    name: str
    normalized_name: str
    entity_type: str
    domain: str
    status: str
    collection_scopes: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    selection: SelectionTrace


class RelationContext(BaseModel):
    relation_id: str
    legacy_id: str | None = None
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    domain: str
    status: str
    collection_scopes: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    selection: SelectionTrace


class ClaimContext(BaseModel):
    claim_id: str
    legacy_id: str | None = None
    entity_id: str | None = None
    relation_id: str | None = None
    subject: str
    predicate: str
    object_value: str
    claim_type: str
    statement: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: str
    collection_scopes: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    selection: SelectionTrace


class KnowledgeClaimBundle(BaseModel):
    """A Claim is always emitted with its support and locatable provenance."""

    claim: ClaimContext
    entity: EntityContext | None = None
    relation: RelationContext | None = None
    evidence: list[EvidenceContext] = Field(min_length=1)
    chunks: list[ChunkContext] = Field(min_length=1)
    documents: list[DocumentContext] = Field(min_length=1)
    sources: list[SourceContext] = Field(min_length=1)
    selection: SelectionTrace


class KnowledgeContext(BaseModel):
    """The Runtime's single knowledge payload.

    Claim bundles are intentionally the only representation.  A former set of
    flattened entity/claim/evidence lists duplicated the exact evidence text
    emitted by each bundle, which made the reported budget diverge from the
    actual Runtime payload.
    """

    claim_bundles: list[KnowledgeClaimBundle] = Field(default_factory=list)


class ArtifactContext(BaseModel):
    task_artifacts: list[ArtifactReferenceContext] = Field(default_factory=list)
    project_artifacts: list[ArtifactReferenceContext] = Field(default_factory=list)


class ConstraintContext(BaseModel):
    project_scope: str
    domain: str
    collection_scopes: list[str] = Field(default_factory=list)
    permitted_knowledge_statuses: list[str] = Field(default_factory=lambda: ["published"])
    time_limit_mode: Literal["version_only", "temporal_not_supported"] = "version_only"
    source_version_required: bool = True
    max_context_tokens: int
    cross_project_allowed: bool = False
    cross_collection_allowed: bool = False


class ToolCapability(BaseModel):
    name: str
    description: str
    allowed_inputs: list[str] = Field(default_factory=list)
    can_execute: Literal[False] = False


class ToolContext(BaseModel):
    capabilities: list[ToolCapability] = Field(default_factory=list)


class TokenUsage(BaseModel):
    budget: int
    used: int
    estimation_method: Literal["characters_div_4"] = "characters_div_4"
    payload_representation: Literal["runtime_context_v1"] = "runtime_context_v1"


class ContextDiagnostics(BaseModel):
    knowledge_coverage: Literal["available", "empty_core", "no_scope", "no_relevant_claims"]
    structured_candidates: int = Field(default=0, ge=0)
    vector_candidates: int = Field(default=0, ge=0)
    graph_candidates: int = Field(default=0, ge=0)
    verified_claim_bundles: int = Field(default=0, ge=0)
    selected_claim_bundles: int = Field(default=0, ge=0)
    dropped_for_budget: int = Field(default=0, ge=0)
    notices: list[str] = Field(default_factory=list)


class ContextSnapshotItem(BaseModel):
    section: str
    item_type: str
    item_id: str
    parent_item_id: str | None = None
    rank: int | None = None
    score: float | None = None
    selected_reason: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    estimated_tokens: int = Field(default=0, ge=0)


class ContextPackage(BaseModel):
    snapshot_id: str
    package_schema_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    reading_format: ReadingFormat = 'legacy'
    builder_version: str = "phase2-context-builder-v1"
    created_at: str
    request_fingerprint: str
    project: ProjectContext
    task: TaskContext
    memory: MemoryContext
    knowledge: KnowledgeContext
    artifacts: ArtifactContext
    constraints: ConstraintContext
    tools: ToolContext
    token_usage: TokenUsage
    diagnostics: ContextDiagnostics
    package_sha256: str = ""
    retrieval_audit: RetrievalAudit | None = None

    @model_validator(mode="after")
    def validate_audit_version(self) -> ContextPackage:
        if (self.package_schema_version in {'1.1', '1.2'}) != (self.retrieval_audit is not None):
            raise ValueError("Context schema 1.1/1.2 requires retrieval audit; 1.0 must omit it.")
        return self


def runtime_context_payload(package: ContextPackage) -> dict[str, Any]:
    """Estimate legacy packages as before; versioned packages use exact research input.

    Legacy snapshots predate unified reading accounting and exclude the audit
    envelope. Versioned research views also include the snapshot ID and hash.
    """

    if package.reading_format != 'legacy':
        return research_payload(package)

    def payload(value: BaseModel) -> dict[str, Any]:
        return value.model_dump(mode="json", exclude_defaults=True, exclude_none=True)

    return {
        "project": payload(package.project),
        "task": payload(package.task),
        "memory": payload(package.memory),
        "knowledge": payload(package.knowledge),
        "artifacts": payload(package.artifacts),
        "constraints": payload(package.constraints),
        "tools": payload(package.tools),
    }


def runtime_context_token_count(package: ContextPackage) -> int:
    """Estimate tokens from the exact normalized Runtime payload."""

    rendered = json.dumps(
        runtime_context_payload(package),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return max(1, (len(rendered) + 3) // 4)


def canonical_package_sha256(package: ContextPackage) -> str:
    """Return the canonical audit digest, excluding its self-referential value."""

    values = package.model_dump(mode="json")
    if package.package_schema_version != '1.2' and package.reading_format == 'legacy':
        values.pop('reading_format', None)
    if package.package_schema_version == "1.0":
        # Historical 1.0 hashes predate this field. Never backfill old snapshots.
        values.pop("retrieval_audit", None)
    values["package_sha256"] = ""
    encoded = json.dumps(
        values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
