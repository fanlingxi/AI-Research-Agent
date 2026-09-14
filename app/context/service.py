"""Task-driven construction of governed, immutable Context Packages."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.context.knowledge_reader import KnowledgeContextReader, RawKnowledgeBundle
from app.context.models import (
    ArtifactContext,
    ArtifactReferenceContext,
    ChunkContext,
    ClaimContext,
    ConstraintContext,
    ContextBuildRequest,
    ContextDiagnostics,
    ContextPackage,
    ContextSnapshotItem,
    DecisionContext,
    DocumentContext,
    EntityContext,
    EvidenceContext,
    KnowledgeClaimBundle,
    KnowledgeContext,
    KnowledgeScopeContext,
    MemoryContext,
    ProjectContext,
    RelationContext,
    SelectionTrace,
    SourceContext,
    TaskContext,
    TokenUsage,
    ToolCapability,
    ToolContext,
    WorkspaceTaskContext,
    canonical_package_sha256,
    runtime_context_token_count,
)
from app.context.neighbors import MAX_ANCHORS, adjacent_bundles
from app.context.query_ranking import rank_query_bundles
from app.context.ranking import RankedKnowledgeBundle, rank_bundles
from app.context.repository import ContextSnapshotRepository
from app.context.retrieval import (
    CandidateReference,
    ContextCandidateRetriever,
    Neo4jContextCandidateRetriever,
    QdrantContextCandidateRetriever,
)
from app.context.retrieval_audit import bundle_bindings, context_retrieval_audit
from app.knowledge.repository import KnowledgeRepository
from app.memory.models import Artifact, MemoryDecision, ProjectMemorySnapshot, WorkspaceTask
from app.retrieval.contracts import validate_candidates
from app.retrieval.coverage import (
    CoreCoverageReranker,
    CoverageReranker,
    coverage_indices,
    selected_coverage,
)
from app.retrieval.policy import MAX_CONTEXT_PREPARATIONS, MAX_EXTERNAL_ATTEMPTS, is_transient_error
from app.retrieval.query_planning import QueryPlanner, task_plan
from app.retrieval.reranking import EvidenceReranker, apply_reranking, input_digest, ranking_input

_TERMINAL_TASK_STATUSES = {"completed", "cancelled"}
_TERMINAL_PROJECT_STATUSES = {"completed", "archived"}


class ContextConflictError(ValueError):
    """The task exists but is not eligible for a new runtime context."""

    def __init__(
        self, message: str, *, preparation_attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.preparation_attempts = preparation_attempts or []


class ContextBudgetTooSmallError(ValueError):
    """The mandatory, normalized Runtime payload exceeds the supplied budget."""


class _ContextInputsChanged(ContextConflictError):
    """Prepared inputs changed before the observation/commit boundary."""


@dataclass(frozen=True)
class _PreparedInputs:
    task: WorkspaceTask
    memory: ProjectMemorySnapshot
    bundles: list[RawKnowledgeBundle]
    has_core_claim: bool
    fingerprint: str


class ContextBuilderService:
    """Build read-only Context Packages from one consistent SQLite snapshot.

    The optional retrievers may only return candidate identifiers. Every
    candidate is rehydrated and validated by :class:`KnowledgeContextReader`
    before it can influence the output.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        vector_retriever: ContextCandidateRetriever | None = None,
        graph_retriever: ContextCandidateRetriever | None = None,
        snapshot_repository: ContextSnapshotRepository | None = None,
        query_planner: QueryPlanner | None = None,
        evidence_reranker: EvidenceReranker | None = None,
        coverage_reranker: CoverageReranker | None = None,
    ) -> None:
        self.repository = repository
        self.memory_repository = repository.memory_repository
        self.database = repository.database
        self.knowledge_reader = KnowledgeContextReader()
        self.vector_retriever = vector_retriever or QdrantContextCandidateRetriever()
        self.graph_retriever = graph_retriever or Neo4jContextCandidateRetriever()
        self.query_planner = query_planner or QueryPlanner()
        self.evidence_reranker = evidence_reranker or EvidenceReranker()
        self.coverage_reranker = coverage_reranker
        self.snapshot_repository = snapshot_repository or ContextSnapshotRepository(
            repository.path, repository.database
        )

    def build(self, request: ContextBuildRequest) -> ContextPackage:
        """Compatibility alias for :meth:`build_context`."""

        return self.build_context(request)

    def preview_context(self, request: ContextBuildRequest) -> ContextPackage:
        """Build the exact Runtime package without persisting a Snapshot."""

        return self._build_context(request, persist=False)

    def build_context(self, request: ContextBuildRequest) -> ContextPackage:
        """Create and persist one immutable Context Package.

        Preparation reads one SQLite snapshot, then closes it before retrieval.
        Final input validation and persistence share one write transaction.
        """

        return self._build_context(request, persist=True)

    def validate_fresh_context_tx(
        self, connection: sqlite3.Connection, package: ContextPackage
    ) -> None:
        """Check a newly prepared rerun input at the atomic queue/link boundary.

        No retrieval or model calls occur here. Historical snapshots remain readable,
        but are not accepted as freshly prepared feedback reruns.
        """
        audit = package.retrieval_audit
        expected = audit.parameters.get("input_sha256") if audit else None
        current = self._read_inputs_tx(connection, ContextBuildRequest(
            task_id=package.task.task_id, project_id=package.project.project_id,
        ))
        if not expected or current.fingerprint != expected:
            raise ContextConflictError("Context changed before feedback rerun was queued.")

    def _build_context(self, request: ContextBuildRequest, *, persist: bool) -> ContextPackage:
        request = request.model_copy(deep=True)
        attempts: list[dict[str, Any]] = []
        planning_cache: dict[str, Any] = {}
        for attempt in range(1, MAX_CONTEXT_PREPARATIONS + 1):
            with self.database.connect() as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                prepared = self._read_inputs_tx(connection, request)
            package, items = self._prepare_context(
                request, prepared, attempt, attempts, planning_cache=planning_cache
            )

            def validate_state(
                connection: sqlite3.Connection, expected: str = prepared.fingerprint,
            ) -> None:
                current = self._read_inputs_tx(connection, request)
                if current.fingerprint != expected:
                    raise _ContextInputsChanged("Context inputs changed during preparation.")

            try:
                if persist:
                    self.snapshot_repository.save(package, items, validate_state=validate_state)
                else:
                    with self.database.connect() as connection:
                        connection.execute("PRAGMA query_only = ON")
                        connection.execute("BEGIN")
                        validate_state(connection)
                return package
            except _ContextInputsChanged:
                attempts.append({
                    "attempt": attempt, "input_sha256": prepared.fingerprint,
                    "outcome": "inputs_changed", "notices": package.diagnostics.notices,
                    'reranking': package.retrieval_audit.parameters.get('reranking'),
                })
            except ContextConflictError as exc:
                exc.preparation_attempts = [*attempts, {
                    "attempt": attempt, "input_sha256": prepared.fingerprint,
                    "outcome": "ineligible", "notices": package.diagnostics.notices,
                }]
                raise
        raise ContextConflictError(
            f"Context inputs changed in all {MAX_CONTEXT_PREPARATIONS} preparation attempts; "
            "no snapshot was saved.", preparation_attempts=attempts,
        )

    def _read_inputs_tx(
        self, connection: sqlite3.Connection, request: ContextBuildRequest
    ) -> _PreparedInputs:
        task = self.memory_repository.read_workspace_task_tx(connection, request.task_id)
        if request.project_id is not None and request.project_id != task.project_id:
            raise ContextConflictError("WorkspaceTask does not belong to the requested Project.")
        memory = self.memory_repository.snapshot_tx(connection, task.project_id)
        self._require_context_eligible(memory, task)
        scopes = [scope.collection_slug for scope in memory.knowledge_scopes]
        bundles = self.knowledge_reader.read_formal_bundles_tx(connection, scopes)
        # Global existence only influences empty-core coverage when scoped bundles
        # are absent. Unrelated writes must not invalidate an otherwise valid build.
        has_core_claim = bool(scopes and not bundles and connection.execute(
            "SELECT 1 FROM claims WHERE status = 'published' LIMIT 1"
        ).fetchone())
        fingerprint = _fingerprint({
            "task": task.model_dump(mode="json"), "memory": memory.model_dump(mode="json"),
            "bundles": [asdict(bundle) for bundle in bundles], "has_core_claim": has_core_claim,
        })
        return _PreparedInputs(task, memory, bundles, has_core_claim, fingerprint)

    def _prepare_context(
        self, request: ContextBuildRequest, prepared: _PreparedInputs,
        attempt: int, previous_attempts: list[dict[str, Any]],
        *, planning_cache: dict[str, Any] | None = None,
    ) -> tuple[ContextPackage, list[ContextSnapshotItem]]:
        """Retrieve, rank and serialize after the preparation connection is closed."""

        task, memory_snapshot, raw_bundles = prepared.task, prepared.memory, prepared.bundles
        scopes = [scope.collection_slug for scope in memory_snapshot.knowledge_scopes]
        query = _task_query(task, memory_snapshot)
        plan = None
        query_details = []
        if request.query_planning != "off":
            question = "\n".join(part for part in (task.title, task.goal) if part)
            planning_cache = planning_cache if planning_cache is not None else {}
            if not scopes or not raw_bundles:
                plan = task_plan(question, status="fallback", reason="no_verified_scope")
            elif request.query_planning == "task-v1":
                plan = task_plan(question)
            elif "plan" not in planning_cache:
                plan = self.query_planner.plan(question)
                planning_cache.update(question=question, plan=plan)
            elif planning_cache["question"] == question:
                plan = planning_cache["plan"]
            else:
                plan = task_plan(question, status="fallback", reason="question_changed_after_plan")
            if plan.status != "fallback":
                query = plan.queries[0]
        candidate_references, notices = self._optional_candidates(
            request,
            query=query,
            collection_scopes=scopes,
            raw_bundles=raw_bundles,
        )
        candidate_audits = validate_candidates(
            candidate_references, bundle_bindings(raw_bundles)
        )
        ranked_bundles = rank_bundles(
            raw_bundles,
            query=query,
            candidate_references=candidate_references,
            candidate_audits=candidate_audits,
            strategy=request.retrieval_strategy,
        )
        if plan is not None and plan.status != "fallback":
            query_candidates, query_audits = [candidate_references], [candidate_audits]
            for extra_query in plan.queries[1:]:
                refs, extra_notices = self._optional_candidates(
                    request, query=extra_query, collection_scopes=scopes,
                    raw_bundles=raw_bundles,
                )
                query_candidates.append(refs)
                query_audits.append(validate_candidates(refs, bundle_bindings(raw_bundles)))
                notices.extend(f"query_{len(query_candidates)-1}:{n}" for n in extra_notices)
            ranked_bundles, query_details = rank_query_bundles(
                raw_bundles, plan.queries, query_candidates, query_audits,
                hybrid=request.enable_vector_candidates or request.enable_graph_candidates,
            )
            candidate_references = [ref for refs in query_candidates for ref in refs]
            candidate_audits = [a for audits in query_audits for a in audits]
        elif plan is not None:
            notices.append(f"query_planning_fallback:{plan.reason}")
        rerank_record = None
        if request.evidence_reranking != 'off':
            question = '\n'.join(part for part in (task.title, task.goal) if part)
            payload = ranking_input(question, ranked_bundles)
            candidate_claim_ids = [r.bundle.claim['id'] for r in ranked_bundles]
            cache = planning_cache if planning_cache is not None else {}
            original_ranks = {r.bundle.claim['id']: r.rank for r in ranked_bundles}
            if '_rerank' not in cache:
                ranker = ((self.coverage_reranker or (CoreCoverageReranker()
                           if request.evidence_reranking == 'coverage-v2' else CoverageReranker()))
                          if request.evidence_reranking in {'coverage-v1', 'coverage-v2'}
                          else self.evidence_reranker)
                record = ranker.rank(payload)
                # Validate even injected adapters against this exact candidate input.
                if record.get('input_sha256') != input_digest(payload):
                    raise ContextConflictError('Reranker input binding mismatch')
                if (request.evidence_reranking in {'coverage-v1', 'coverage-v2'}
                        and record['status'] == 'ranked'):
                    if coverage_indices(payload, record.get('parts')) != record.get('indices'):
                        raise ContextConflictError('Coverage ordering binding mismatch')
                record = {**record, 'candidate_claim_ids': candidate_claim_ids}
                cache['_rerank'] = (prepared.fingerprint, record)
            fingerprint, record = cache['_rerank']
            if (fingerprint != prepared.fingerprint
                    or record.get('input_sha256') != input_digest(payload)
                    or record.get('candidate_claim_ids') != candidate_claim_ids):
                rerank_record = {'status': 'fallback', 'reason': 'inputs_changed_after_rerank',
                                 'model_calls': 0, 'usage': None, 'previous_attempt': record}
            else:
                rerank_record = {**record, 'original_ranks': original_ranks}
                ranked_bundles = apply_reranking(ranked_bundles, record)
                rerank_record['reranked_claim_ids'] = [r.bundle.claim['id'] for r in ranked_bundles]
        neighbor_groups = None
        if request.context_neighbors == "adjacent-v1":
            ranked_bundles, neighbor_groups = adjacent_bundles(raw_bundles, ranked_bundles)
        coverage = self._knowledge_coverage(
            has_core_claim=prepared.has_core_claim,
            scopes=scopes,
            raw_bundles=raw_bundles,
            ranked_bundles=ranked_bundles,
        )
        package, items = self._assemble_package(
            request,
            memory_snapshot=memory_snapshot,
            task=task,
            ranked_bundles=ranked_bundles,
            coverage=coverage,
            vector_candidates=sum(
                reference.channel == "vector" for reference in candidate_references
            ),
            graph_candidates=sum(
                reference.channel == "graph" for reference in candidate_references
            ),
            verified_bundles=len(raw_bundles),
            notices=notices,
            neighbor_groups=neighbor_groups,
        )

        audit = context_retrieval_audit(
            request, package, raw_bundles, ranked_bundles, candidate_audits
        )
        if rerank_record is not None:
            if request.evidence_reranking in {'coverage-v1', 'coverage-v2'}:
                rerank_record['selected_coverage'] = selected_coverage(
                    rerank_record, {b.claim.claim_id for b in package.knowledge.claim_bundles}
                )
            audit.parameters['reranking'] = rerank_record
        audit.parameters.update({
            "consistency_policy": "prepare-revalidate-v1",
            "input_sha256": prepared.fingerprint,
            "preparation_attempts": attempt,
            "previous_attempts": previous_attempts,
            "max_preparation_attempts": MAX_CONTEXT_PREPARATIONS,
            "max_external_attempts_per_channel": MAX_EXTERNAL_ATTEMPTS,
        })
        if plan is not None:
            if plan.status != "fallback":
                audit.strategy_id = f"project-context-query-{request.query_planning}"
                audit.parameters = {key: value for key, value in audit.parameters.items()
                                    if key not in {"lexical_weight", "semantic_weight",
                                                   "owner_weight", "confidence_weight",
                                                   "completeness_weight", "term_limit",
                                                   "projection_aggregation",
                                                   "confidence_fallback",
                                                   "evidence_saturation_count"}}
            audit.parameters.update({
                "query_planning": plan.model_dump(mode="json"),
                "query_rankings": query_details,
                "query_fusion": {"rrf_k": 60, "per_query_limit": 40},
                "planning_attempt": (planning_cache.get("plan").model_dump(mode="json")
                                     if planning_cache and planning_cache.get("plan") else None),
                "planning_usage_scope": "pre_snapshot_not_agent_run",
                "max_planner_calls_per_build": 1,
                "max_external_attempts_per_query_channel": MAX_EXTERNAL_ATTEMPTS,
                "max_queries": 4,
                "channel_rank_kind": "per_query_returned_candidate_position",
            })
        if neighbor_groups is not None:
            selected_ids = {b.claim.claim_id for b in package.knowledge.claim_bundles}
            audit.strategy_id += "+adjacent-v1"
            audit.parameters["context_neighbors"] = {
                "strategy": "adjacent-v1", "max_anchors": MAX_ANCHORS,
                "neighbors_per_anchor": 2, "recursive": False,
                "budget_policy": "whole_new_group_prefix",
                "groups": [{**group, "selected": set(group["claim_ids"]) <= selected_ids}
                           for group in neighbor_groups],
            }
        package = ContextPackage.model_validate({
            **package.model_dump(mode="json"),
            "package_schema_version": '1.2' if request.reading_format != 'legacy' else '1.1',
            "retrieval_audit": audit.model_dump(mode="json"),
        })
        package = package.model_copy(
            update={"package_sha256": canonical_package_sha256(package)}
        )

        return package, items

    def _optional_candidates(
        self,
        request: ContextBuildRequest,
        *,
        query: str,
        collection_scopes: list[str],
        raw_bundles: list[RawKnowledgeBundle],
    ) -> tuple[list[CandidateReference], list[str]]:
        references: list[CandidateReference] = []
        notices: list[str] = []
        document_ids = {
            evidence.document["id"] for bundle in raw_bundles for evidence in bundle.evidence
        }
        if not request.enable_vector_candidates and not request.enable_graph_candidates:
            return [], []
        if not collection_scopes or not document_ids:
            return [], ["external_candidates_skipped_no_verified_scope"]
        for enabled, retriever, channel in (
            (request.enable_vector_candidates, self.vector_retriever, "vector"),
            (request.enable_graph_candidates, self.graph_retriever, "graph"),
        ):
            if not enabled:
                continue
            if retriever is None:
                notices.append(f"{channel}_not_configured")
                continue
            for attempt in range(1, MAX_EXTERNAL_ATTEMPTS + 1):
                try:
                    returned = retriever.retrieve(
                        query,
                        collection_scopes=list(collection_scopes),
                        allowed_document_ids=set(document_ids),
                        limit=40,
                    )
                    if request.query_planning != "off":
                        returned = returned[:40]
                    channel_references = [ref for ref in returned if ref.channel == channel]
                except Exception as exc:
                    transient = is_transient_error(exc)
                    notices.append(
                        f"{channel}_attempt_{attempt}_"
                        f"{'transient_failure' if transient else 'failure'}"
                    )
                    if transient and attempt < MAX_EXTERNAL_ATTEMPTS:
                        continue
                    notices.append(f"{channel}_unavailable")
                    break
                references.extend(channel_references)
                break
        return references, notices

    @staticmethod
    def _require_context_eligible(snapshot: ProjectMemorySnapshot, task: WorkspaceTask) -> None:
        if snapshot.project.status in _TERMINAL_PROJECT_STATUSES:
            raise ContextConflictError(f"Project is {snapshot.project.status}.")
        if task.status in _TERMINAL_TASK_STATUSES:
            raise ContextConflictError(f"WorkspaceTask is {task.status}.")

    @staticmethod
    def _knowledge_coverage(
        *,
        has_core_claim: bool,
        scopes: list[str],
        raw_bundles: list[RawKnowledgeBundle],
        ranked_bundles: list[RankedKnowledgeBundle],
    ) -> str:
        if not scopes:
            return "no_scope"
        if raw_bundles:
            return "available" if ranked_bundles else "no_relevant_claims"
        return "no_relevant_claims" if has_core_claim else "empty_core"

    def _assemble_package(
        self,
        request: ContextBuildRequest,
        *,
        memory_snapshot: ProjectMemorySnapshot,
        task: WorkspaceTask,
        ranked_bundles: list[RankedKnowledgeBundle],
        coverage: str,
        vector_candidates: int,
        graph_candidates: int,
        verified_bundles: int,
        notices: list[str],
        neighbor_groups: list[dict] | None = None,
    ) -> tuple[ContextPackage, list[ContextSnapshotItem]]:
        project_context = _project_context(memory_snapshot)
        task_context = _task_context(task)
        constraints = ConstraintContext(
            project_scope=memory_snapshot.project.id,
            domain=memory_snapshot.project.domain,
            collection_scopes=[scope.collection_slug for scope in memory_snapshot.knowledge_scopes],
            max_context_tokens=request.max_tokens,
        )
        tools = ToolContext(
            capabilities=[
                ToolCapability(
                    name="context",
                    description="Read-only.",
                    allowed_inputs=["task_id", "project_id"],
                ),
            ]
        )
        memory = _memory_context(memory_snapshot, task.id)
        artifacts = _artifact_context(memory_snapshot.artifacts, task.id)
        selected_bundles = [_knowledge_bundle(item) for item in ranked_bundles]
        selected_bundles = [
            _with_estimated_tokens(bundle, _estimate_tokens(bundle)) for bundle in selected_bundles
        ]
        diagnostic_notices = list(notices)
        if coverage == "empty_core":
            diagnostic_notices.append(
                "No published Core claims are available; legacy tables were not used."
            )
        elif coverage == "no_scope":
            diagnostic_notices.append("Project has no explicit Knowledge Collection scope.")
        elif coverage == "no_relevant_claims":
            diagnostic_notices.append("No eligible formal Claim matched the task-driven query.")
        request_fingerprint = _fingerprint(
            {
                "request": request.model_dump(mode="json"),
                "project_id": memory_snapshot.project.id,
                "project_revision": memory_snapshot.project.revision,
                "task_revision": task.revision,
                "collection_scopes": constraints.collection_scopes,
            }
        )
        snapshot_id = request.snapshot_id or f"context-snapshot-{uuid4().hex}"
        created_at = _now()
        dropped_for_budget = 0
        dropped_memory = 0
        dropped_artifacts = 0
        group_by_claim = {key: set(group["claim_ids"]) for group in (neighbor_groups or [])
                          for key in group["claim_ids"]}

        def candidate_package() -> ContextPackage:
            candidate_notices = list(diagnostic_notices)
            if dropped_for_budget:
                candidate_notices.append(
                    "Lower-ranked Claim bundles were omitted to respect the budget."
                )
            if dropped_artifacts:
                candidate_notices.append(
                    "Lower-priority Artifact references were omitted to respect the budget."
                )
            if dropped_memory:
                candidate_notices.append(
                    "Lower-priority Memory records were omitted to respect the budget."
                )
            return ContextPackage(
                snapshot_id=snapshot_id,
                reading_format=request.reading_format,
                created_at=created_at,
                request_fingerprint=request_fingerprint,
                project=project_context,
                task=task_context,
                memory=memory,
                knowledge=_knowledge_context(selected_bundles),
                artifacts=artifacts,
                constraints=constraints,
                tools=tools,
                token_usage=TokenUsage(budget=request.max_tokens, used=0),
                diagnostics=ContextDiagnostics(
                    knowledge_coverage=coverage,
                    structured_candidates=len(ranked_bundles),
                    vector_candidates=vector_candidates,
                    graph_candidates=graph_candidates,
                    verified_claim_bundles=verified_bundles,
                    selected_claim_bundles=len(selected_bundles),
                    dropped_for_budget=dropped_for_budget,
                    notices=candidate_notices,
                ),
            )

        # Claim bundles remain indivisible.  The loop always measures the
        # final normalized Runtime payload, not a collection of partial costs.
        while True:
            package = _finalize_runtime_usage(candidate_package())
            if package.token_usage.used <= request.max_tokens:
                return package, _snapshot_items(package)
            if selected_bundles:
                last = selected_bundles[-1].claim.claim_id
                dropped = group_by_claim.get(last, {last})
                before = len(selected_bundles)
                selected_bundles = [b for b in selected_bundles if b.claim.claim_id not in dropped]
                dropped_for_budget += before - len(selected_bundles)
                continue
            if artifacts.project_artifacts:
                artifacts.project_artifacts.pop()
                dropped_artifacts += 1
                continue
            if artifacts.task_artifacts:
                artifacts.task_artifacts.pop()
                dropped_artifacts += 1
                continue
            if memory.accepted_decisions:
                memory.accepted_decisions.pop()
                dropped_memory += 1
                continue
            if memory.open_workspace_tasks:
                memory.open_workspace_tasks.pop()
                dropped_memory += 1
                continue
            raise ContextBudgetTooSmallError(
                "The required Task, Project, Constraint, and Tool Context is "
                f"{package.token_usage.used} tokens, exceeding max_tokens={request.max_tokens}."
            )


def _project_context(snapshot: ProjectMemorySnapshot) -> ProjectContext:
    project = snapshot.project
    scopes = [
        KnowledgeScopeContext(
            collection_slug=scope.collection_slug,
            created_at=scope.created_at,
            selection=_trace(
                "required explicit project knowledge scope",
                provenance={"project_id": project.id, "collection_slug": scope.collection_slug},
            ),
        )
        for scope in snapshot.knowledge_scopes
    ]
    return ProjectContext(
        project_id=project.id,
        name=project.name,
        goal=project.goal,
        domain=project.domain,
        status=project.status,
        metadata=project.metadata,
        revision=project.revision,
        created_at=project.created_at,
        updated_at=project.updated_at,
        knowledge_scopes=scopes,
        selection=_trace(
            "required project scope",
            provenance={"record_type": "project"},
        ),
    )


def _task_context(task: WorkspaceTask) -> TaskContext:
    expected_output = task.metadata.get("expected_output")
    return TaskContext(
        task_id=task.id,
        project_id=task.project_id,
        title=task.title,
        goal=task.goal,
        status=task.status,
        priority=task.priority,
        expected_output=expected_output if isinstance(expected_output, str) else None,
        metadata=task.metadata,
        revision=task.revision,
        created_at=task.created_at,
        updated_at=task.updated_at,
        selection=_trace(
            "required current workspace task",
            provenance={"record_type": "workspace_task"},
        ),
    )


def _memory_context(snapshot: ProjectMemorySnapshot, task_id: str) -> MemoryContext:
    task_items = [
        WorkspaceTaskContext(
            task_id=item.id,
            project_id=item.project_id,
            title=item.title,
            goal=item.goal,
            status=item.status,
            priority=item.priority,
            metadata=item.metadata,
            revision=item.revision,
            created_at=item.created_at,
            updated_at=item.updated_at,
            selection=_trace(
                "current task also appears in open project work"
                if item.id == task_id
                else "open project workspace task",
                provenance={"task_id": item.id, "revision": item.revision},
            ),
        )
        for item in snapshot.workspace_tasks
        if item.id != task_id
    ]
    decision_items = [_decision_context(item, task_id) for item in snapshot.decisions]
    return MemoryContext(
        open_workspace_tasks=task_items,
        accepted_decisions=decision_items,
        required_constraints=_project_constraints(snapshot.project.metadata),
    )


def _decision_context(item: MemoryDecision, task_id: str) -> DecisionContext:
    return DecisionContext(
        decision_id=item.id,
        project_id=item.project_id,
        task_id=item.task_id,
        summary=item.summary,
        rationale=item.rationale,
        impact=item.impact,
        status=item.status,
        metadata=item.metadata,
        revision=item.revision,
        created_at=item.created_at,
        updated_at=item.updated_at,
        selection=_trace(
            "accepted decision directly linked to current task"
            if item.task_id == task_id
            else "accepted project decision",
            provenance={"decision_id": item.id, "revision": item.revision},
        ),
    )


def _artifact_context(artifacts: list[Artifact], task_id: str) -> ArtifactContext:
    direct = [
        _artifact_reference(item, "artifact directly linked to current task")
        for item in artifacts
        if item.task_id == task_id
    ]
    project = [
        _artifact_reference(item, "active project artifact")
        for item in artifacts
        if item.task_id != task_id
    ]
    return ArtifactContext(task_artifacts=direct, project_artifacts=project)


def _artifact_reference(item: Artifact, reason: str) -> ArtifactReferenceContext:
    return ArtifactReferenceContext(
        artifact_id=item.id,
        project_id=item.project_id,
        task_id=item.task_id,
        type=item.type,
        reference=item.reference,
        version=item.version,
        status=item.status,
        supersedes_artifact_id=item.supersedes_artifact_id,
        metadata=item.metadata,
        revision=item.revision,
        created_at=item.created_at,
        updated_at=item.updated_at,
        selection=_trace(reason, provenance={"artifact_id": item.id, "revision": item.revision}),
    )


def _knowledge_bundle(ranked: RankedKnowledgeBundle) -> KnowledgeClaimBundle:
    raw = ranked.bundle
    claim_trace = _knowledge_trace(ranked, {"claim_id": raw.claim["id"]})
    claim = ClaimContext(
        claim_id=raw.claim["id"],
        legacy_id=raw.claim["legacy_id"],
        entity_id=raw.claim["entity_id"],
        relation_id=raw.claim["relation_id"],
        subject=raw.claim["subject"],
        predicate=raw.claim["predicate"],
        object_value=raw.claim["object_value"],
        claim_type=raw.claim["claim_type"],
        statement=raw.claim["statement"],
        confidence=raw.claim["confidence"],
        status=raw.claim["status"],
        collection_scopes=raw.collection_scopes,
        created_at=raw.claim["created_at"],
        updated_at=raw.claim["updated_at"],
        selection=claim_trace,
    )
    entity = _entity_context(raw.entity, ranked) if raw.entity else None
    relation = _relation_context(raw.relation, ranked) if raw.relation else None
    evidence_items: list[EvidenceContext] = []
    chunks: dict[str, ChunkContext] = {}
    documents: dict[str, DocumentContext] = {}
    sources: dict[str, SourceContext] = {}
    for item in raw.evidence:
        trace = _knowledge_trace(
            ranked,
            {
                "claim_id": raw.claim["id"],
                "evidence_id": item.evidence["id"],
                "source_id": item.source["id"],
                "chunk_id": item.chunk["id"],
            },
        )
        evidence_items.append(
            EvidenceContext(
                evidence_id=item.evidence["id"],
                claim_id=raw.claim["id"],
                source_id=item.evidence["source_id"],
                chunk_id=item.evidence["chunk_id"],
                quote=item.evidence["quote"],
                quote_sha256=item.evidence["quote_sha256"],
                location=item.evidence["location"],
                selection=trace,
            )
        )
        chunks.setdefault(
            item.chunk["id"],
            ChunkContext(
                chunk_id=item.chunk["id"],
                document_id=item.chunk["document_id"],
                chunk_index=item.chunk["chunk_index"],
                page_start=item.chunk["page_start"],
                page_end=item.chunk["page_end"],
                content=item.chunk["content"],
                content_sha256=item.chunk["content_sha256"],
                location=item.chunk["location"],
                selection=trace,
            ),
        )
        documents.setdefault(
            item.document["id"],
            DocumentContext(
                document_id=item.document["id"],
                source_id=item.document["source_id"],
                title=item.document["title"],
                source=item.document["source"],
                pages=item.document["pages"],
                content_sha256=item.document["content_sha256"],
                content_status=item.document["content_status"],
                parser_version=item.document["parser_version"],
                parsed_at=item.document["parsed_at"],
                selection=trace,
            ),
        )
        sources.setdefault(
            item.source["id"],
            SourceContext(
                source_id=item.source["id"],
                source_type=item.source["source_type"],
                title=item.source["title"],
                uri=item.source["uri"],
                canonical_uri=item.source["canonical_uri"],
                version=item.source["version"],
                content_sha256=item.source["content_sha256"],
                created_at=item.source["created_at"],
                updated_at=item.source["updated_at"],
                selection=trace,
            ),
        )
    return KnowledgeClaimBundle(
        claim=claim,
        entity=entity,
        relation=relation,
        evidence=evidence_items,
        chunks=list(chunks.values()),
        documents=list(documents.values()),
        sources=list(sources.values()),
        selection=claim_trace,
    )


def _entity_context(item: dict[str, Any], ranked: RankedKnowledgeBundle) -> EntityContext:
    return EntityContext(
        entity_id=item["id"],
        legacy_id=item["legacy_id"],
        name=item["name"],
        normalized_name=item["normalized_name"],
        entity_type=item["entity_type"],
        domain=item["domain"],
        status=item["status"],
        collection_scopes=item["collection_scopes"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        selection=_knowledge_trace(ranked, {"entity_id": item["id"]}),
    )


def _relation_context(item: dict[str, Any], ranked: RankedKnowledgeBundle) -> RelationContext:
    return RelationContext(
        relation_id=item["id"],
        legacy_id=item["legacy_id"],
        source_entity_id=item["source_entity_id"],
        target_entity_id=item["target_entity_id"],
        relation_type=item["relation_type"],
        domain=item["domain"],
        status=item["status"],
        collection_scopes=item["collection_scopes"],
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        selection=_knowledge_trace(ranked, {"relation_id": item["id"]}),
    )


def _knowledge_context(bundles: list[KnowledgeClaimBundle]) -> KnowledgeContext:
    return KnowledgeContext(claim_bundles=bundles)


def _with_estimated_tokens(item: BaseModel, estimated_tokens: int) -> Any:
    selection = item.selection.model_copy(update={"estimated_tokens": estimated_tokens})
    return item.model_copy(update={"selection": selection})


def _knowledge_trace(ranked: RankedKnowledgeBundle, provenance: dict[str, Any]) -> SelectionTrace:
    return _trace(
        ranked.selected_reason,
        channels=ranked.channels,
        rank=ranked.rank,
        score=ranked.score,
        score_breakdown=ranked.score_breakdown,
        provenance=provenance,
    )


def _trace(
    reason: str,
    *,
    channels: list[str] | None = None,
    rank: int | None = None,
    score: float | None = None,
    score_breakdown: dict[str, float] | None = None,
    provenance: dict[str, Any] | None = None,
) -> SelectionTrace:
    return SelectionTrace(
        selected_reason=reason,
        retrieval_channels=channels or [],
        rank=rank,
        score=score,
        score_breakdown=score_breakdown or {},
        provenance=provenance or {},
    )


def _task_query(task: WorkspaceTask, snapshot: ProjectMemorySnapshot) -> str:
    return "\n".join(
        part
        for part in (task.title, task.goal, snapshot.project.goal, snapshot.project.domain)
        if part
    )


def _project_constraints(metadata: dict[str, Any]) -> list[str]:
    constraints = metadata.get("constraints")
    if isinstance(constraints, str) and constraints.strip():
        return [constraints.strip()]
    if isinstance(constraints, list):
        return [str(item).strip() for item in constraints if str(item).strip()]
    return []


def _estimate_tokens(value: BaseModel) -> int:
    rendered = json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return max(1, (len(rendered) + 3) // 4)


def _fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _finalize_runtime_usage(package: ContextPackage) -> ContextPackage:
    """Set accounting and audit hash from the exact Runtime representation."""

    used = runtime_context_token_count(package)
    package = package.model_copy(
        update={"token_usage": package.token_usage.model_copy(update={"used": used})}
    )
    return package.model_copy(update={"package_sha256": canonical_package_sha256(package)})


def _snapshot_items(package: ContextPackage) -> list[ContextSnapshotItem]:
    items = [
        _snapshot_item("project", "project", package.project.project_id, package.project.selection),
        _snapshot_item("task", "workspace_task", package.task.task_id, package.task.selection),
    ]
    items.extend(
        _snapshot_item("scope", "knowledge_scope", item.collection_slug, item.selection)
        for item in package.project.knowledge_scopes
    )
    items.extend(
        _snapshot_item("memory", "workspace_task", item.task_id, item.selection)
        for item in package.memory.open_workspace_tasks
    )
    items.extend(
        _snapshot_item("memory", "decision", item.decision_id, item.selection)
        for item in package.memory.accepted_decisions
    )
    items.extend(
        _snapshot_item("artifact", "artifact", item.artifact_id, item.selection)
        for item in package.artifacts.task_artifacts + package.artifacts.project_artifacts
    )
    for bundle in package.knowledge.claim_bundles:
        items.append(
            _snapshot_item("knowledge", "claim", bundle.claim.claim_id, bundle.claim.selection)
        )
        if bundle.entity:
            items.append(
                _snapshot_item(
                    "knowledge", "entity", bundle.entity.entity_id, bundle.entity.selection
                )
            )
        if bundle.relation:
            items.append(
                _snapshot_item(
                    "knowledge",
                    "relation",
                    bundle.relation.relation_id,
                    bundle.relation.selection,
                )
            )
        items.extend(
            _snapshot_item(
                "knowledge", "evidence", item.evidence_id, item.selection, bundle.claim.claim_id
            )
            for item in bundle.evidence
        )
        items.extend(
            _snapshot_item(
                "knowledge", "chunk", item.chunk_id, item.selection, bundle.claim.claim_id
            )
            for item in bundle.chunks
        )
        items.extend(
            _snapshot_item(
                "knowledge", "document", item.document_id, item.selection, bundle.claim.claim_id
            )
            for item in bundle.documents
        )
        items.extend(
            _snapshot_item(
                "knowledge", "source", item.source_id, item.selection, bundle.claim.claim_id
            )
            for item in bundle.sources
        )
    return _deduplicate_snapshot_items(items)


def _snapshot_item(
    section: str,
    item_type: str,
    item_id: str,
    trace: SelectionTrace,
    parent_item_id: str | None = None,
) -> ContextSnapshotItem:
    return ContextSnapshotItem(
        section=section,
        item_type=item_type,
        item_id=item_id,
        parent_item_id=parent_item_id,
        rank=trace.rank,
        score=trace.score,
        selected_reason=trace.selected_reason,
        provenance=trace.provenance,
        estimated_tokens=trace.estimated_tokens,
    )


def _deduplicate_snapshot_items(items: list[ContextSnapshotItem]) -> list[ContextSnapshotItem]:
    unique: dict[tuple[str, str, str], ContextSnapshotItem] = {}
    for item in items:
        unique.setdefault((item.section, item.item_type, item.item_id), item)
    return list(unique.values())


def _now() -> str:
    return datetime.now(UTC).isoformat()
