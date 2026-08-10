"""Composition-only reads for the Project Workspace HTTP surface."""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.agent.repository import AgentRunRepository
from app.agent.service import AgentRunService
from app.context.models import ContextBuildRequest, ContextPackage
from app.context.repository import ContextSnapshotRepository
from app.context.service import ContextBuilderService
from app.knowledge.repository import KnowledgeRepository
from app.memory.models import Artifact
from app.workspace.schemas import (
    AgentRunTraceProjection,
    ArtifactContentProjection,
    ContextPreviewProjection,
    ContextProjectionRequest,
    ContextSnapshotItemPage,
    ContextSnapshotSummary,
    EvidenceReferenceProjection,
    WorkspaceDashboardProjection,
)


class WorkspaceProjectionConflictError(ValueError):
    """A UI projection cannot safely resolve its declared provenance."""


class WorkspaceProjectionService:
    """Expose existing domain facts without becoming a new source of truth.

    This service deliberately delegates to Memory, Context, and Agent
    repositories. It does not access checkpoints, external projections, files,
    or raw ContextPackage payloads from an HTTP response.
    """

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        context_builder: ContextBuilderService | None = None,
        agent_run_service: AgentRunService | None = None,
    ) -> None:
        self.repository = repository
        self.memory_repository = repository.memory_repository
        self.context_builder = context_builder or ContextBuilderService(repository)
        self.snapshot_repository: ContextSnapshotRepository = (
            self.context_builder.snapshot_repository
        )
        self.agent_run_service = agent_run_service or AgentRunService(
            repository, context_builder=self.context_builder
        )
        self.agent_repository: AgentRunRepository = self.agent_run_service.repository

    def dashboard(self, *, limit: int) -> WorkspaceDashboardProjection:
        """Return bounded current work, derived only from business records."""

        projects = self.memory_repository.list_projects(include_archived=False)
        active_projects = [project for project in projects if project.status == "active"]
        tasks = [
            task
            for project in projects
            for task in self.memory_repository.list_workspace_tasks(
                project.id, include_closed=True
            )
        ]
        artifacts = [
            artifact
            for project in projects
            for artifact in self.memory_repository.list_artifacts(
                project.id, include_inactive=True
            )
        ]
        proposals = [
            proposal
            for project in projects
            for proposal in self.memory_repository.list_proposals(
                project.id, include_closed=False
            )
            if proposal.status == "proposed"
        ]
        recent_runs, _ = self.agent_repository.list_runs(limit=limit)
        return WorkspaceDashboardProjection(
            active_projects=active_projects[:limit],
            recent_workspace_tasks=sorted(
                tasks, key=lambda item: (item.updated_at, item.id), reverse=True
            )[:limit],
            recent_agent_runs=recent_runs,
            pending_memory_proposals=sorted(
                proposals, key=lambda item: (item.updated_at, item.id), reverse=True
            )[:limit],
            recent_artifacts=sorted(
                artifacts, key=lambda item: (item.updated_at, item.id), reverse=True
            )[:limit],
        )

    def preview_context(
        self,
        project_id: str,
        task_id: str,
        request: ContextProjectionRequest,
    ) -> ContextPreviewProjection:
        self._require_project_task(project_id, task_id)
        package = self.context_builder.preview_context(
            ContextBuildRequest(
                project_id=project_id,
                task_id=task_id,
                max_tokens=request.max_tokens,
            )
        )
        return ContextPreviewProjection(snapshot=_snapshot_summary(package, persisted=False))

    def create_context_snapshot(
        self,
        project_id: str,
        task_id: str,
        request: ContextProjectionRequest,
    ) -> ContextSnapshotSummary:
        self._require_project_task(project_id, task_id)
        package = self.context_builder.build_context(
            ContextBuildRequest(
                project_id=project_id,
                task_id=task_id,
                max_tokens=request.max_tokens,
            )
        )
        return _snapshot_summary(package, persisted=True)

    def get_context_snapshot(self, snapshot_id: str) -> ContextSnapshotSummary:
        return _snapshot_summary(self.snapshot_repository.get(snapshot_id), persisted=True)

    def list_context_snapshot_items(
        self, snapshot_id: str, *, offset: int, limit: int
    ) -> ContextSnapshotItemPage:
        # Verify package integrity before exposing even its separately stored index.
        self.snapshot_repository.get(snapshot_id)
        items = self.snapshot_repository.list_items(snapshot_id)
        page = items[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(items) else None
        return ContextSnapshotItemPage(
            snapshot_id=snapshot_id,
            items=page,
            next_offset=next_offset,
        )

    def get_snapshot_evidence(
        self, snapshot_id: str, evidence_id: str
    ) -> EvidenceReferenceProjection:
        package = self.snapshot_repository.get(snapshot_id)
        for bundle in package.knowledge.claim_bundles:
            evidence = next(
                (item for item in bundle.evidence if item.evidence_id == evidence_id), None
            )
            if evidence is None:
                continue
            chunk = next(
                (item for item in bundle.chunks if item.chunk_id == evidence.chunk_id), None
            )
            if chunk is None:
                raise WorkspaceProjectionConflictError(
                    "ContextSnapshot Evidence has no selected Chunk provenance."
                )
            document = next(
                (item for item in bundle.documents if item.document_id == chunk.document_id),
                None,
            )
            if document is None:
                raise WorkspaceProjectionConflictError(
                    "ContextSnapshot Evidence has no selected Document provenance."
                )
            source = next(
                (item for item in bundle.sources if item.source_id == document.source_id),
                None,
            )
            if source is None or source.source_id != evidence.source_id:
                raise WorkspaceProjectionConflictError(
                    "ContextSnapshot Evidence has no selected Source provenance."
                )
            return EvidenceReferenceProjection(
                snapshot_id=snapshot_id,
                claim={
                    "id": bundle.claim.claim_id,
                    "statement": bundle.claim.statement,
                    "selected_reason": bundle.claim.selection.selected_reason,
                    "provenance": bundle.claim.selection.provenance,
                },
                evidence={
                    "id": evidence.evidence_id,
                    "quote": evidence.quote,
                    "quote_sha256": evidence.quote_sha256,
                    "location": evidence.location,
                    "selected_reason": evidence.selection.selected_reason,
                    "provenance": evidence.selection.provenance,
                },
                chunk={
                    "id": chunk.chunk_id,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "location": chunk.location,
                    "content_sha256": chunk.content_sha256,
                },
                document={
                    "id": document.document_id,
                    "title": document.title,
                    "pages": document.pages,
                    "content_sha256": document.content_sha256,
                    "content_status": document.content_status,
                    "parser_version": document.parser_version,
                },
                source={
                    "id": source.source_id,
                    "title": source.title,
                    "uri": source.uri,
                    "canonical_uri": source.canonical_uri,
                    "version": source.version,
                    "content_sha256": source.content_sha256,
                },
            )
        raise KeyError(f"Evidence {evidence_id} is not selected by ContextSnapshot {snapshot_id}")

    def list_task_runs(
        self,
        project_id: str,
        task_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        self._require_project_task(project_id, task_id)
        return self._list_runs(
            project_id=project_id,
            task_id=task_id,
            limit=limit,
            cursor=cursor,
        )

    def list_project_runs(
        self, project_id: str, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        self.memory_repository.get_project(project_id)
        return self._list_runs(
            project_id=project_id,
            task_id=None,
            limit=limit,
            cursor=cursor,
        )

    def _list_runs(
        self,
        *,
        project_id: str,
        task_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        runs, next_cursor = self.agent_repository.list_runs(
            project_id=project_id,
            task_id=task_id,
            limit=limit,
            cursor=cursor,
        )
        return {
            "items": [item.model_dump() for item in runs],
            "next_cursor": next_cursor,
        }

    def get_run_trace(
        self, run_id: str, *, after_sequence: int, limit: int
    ) -> AgentRunTraceProjection:
        run = self.agent_repository.get_run(run_id)
        events, next_sequence = self.agent_repository.list_events_page(
            run_id, after_sequence=after_sequence, limit=limit
        )
        tool_calls = self.agent_repository.list_tool_calls_for_events(
            run_id, [event.id for event in events]
        )
        token_usage, total_latency_ms = self.agent_repository.trace_totals(run_id)
        return AgentRunTraceProjection(
            run=run,
            events=events,
            tool_calls=tool_calls,
            next_event_sequence=next_sequence,
            token_usage=token_usage,
            total_latency_ms=total_latency_ms,
        )

    def get_artifact_content(self, artifact_id: str) -> ArtifactContentProjection:
        artifact = self.memory_repository.get_artifact(artifact_id)
        output_id = _agent_output_id(artifact)
        run_id = artifact.metadata.get("agent_run_id")
        if not isinstance(run_id, str) or not run_id:
            raise WorkspaceProjectionConflictError(
                "Artifact is missing its AgentRun provenance metadata."
            )
        run = self.agent_repository.get_run(run_id)
        if artifact.project_id != run.project_id or artifact.task_id != run.task_id:
            raise WorkspaceProjectionConflictError(
                "Artifact ownership does not match its AgentRun provenance."
            )
        output = self.agent_repository.get_output(run_id)
        if output.id != output_id:
            raise WorkspaceProjectionConflictError(
                "Artifact reference does not match its AgentRun output."
            )
        if (
            artifact.metadata.get("context_snapshot_id") != run.context_snapshot_id
            or artifact.metadata.get("context_sha256") != run.context_sha256
        ):
            raise WorkspaceProjectionConflictError(
                "Artifact provenance does not match its AgentRun ContextSnapshot."
            )
        return ArtifactContentProjection(
            artifact_id=artifact.id,
            reference=artifact.reference,
            rendered_markdown=output.rendered_text,
            output_id=output.id,
            output_sha256=output.output_sha256,
            run_id=run.id,
            context_snapshot_id=run.context_snapshot_id,
            context_sha256=run.context_sha256,
            validation=output.validation,
        )

    def _require_project_task(self, project_id: str, task_id: str) -> None:
        self.memory_repository.get_project(project_id)
        task = self.memory_repository.get_workspace_task(task_id)
        if task.project_id != project_id:
            raise WorkspaceProjectionConflictError(
                "WorkspaceTask does not belong to the requested Project."
            )


def _snapshot_summary(package: ContextPackage, *, persisted: bool) -> ContextSnapshotSummary:
    item_counts = Counter(
        {
            "knowledge_claim_bundles": len(package.knowledge.claim_bundles),
            "knowledge_evidence": sum(
                len(bundle.evidence) for bundle in package.knowledge.claim_bundles
            ),
            "open_workspace_tasks": len(package.memory.open_workspace_tasks),
            "accepted_decisions": len(package.memory.accepted_decisions),
            "task_artifacts": len(package.artifacts.task_artifacts),
            "project_artifacts": len(package.artifacts.project_artifacts),
        }
    )
    return ContextSnapshotSummary(
        id=package.snapshot_id if persisted else None,
        persisted=persisted,
        project_id=package.project.project_id,
        task_id=package.task.task_id,
        project_revision=package.project.revision,
        task_revision=package.task.revision,
        builder_version=package.builder_version,
        package_schema_version=package.package_schema_version,
        package_sha256=package.package_sha256,
        token_budget=package.token_usage.budget,
        used_tokens=package.token_usage.used,
        collection_scopes=[item.collection_slug for item in package.project.knowledge_scopes],
        item_counts=dict(item_counts),
        diagnostics=package.diagnostics.model_dump(mode="json"),
        created_at=package.created_at,
    )


def _agent_output_id(artifact: Artifact) -> str:
    prefix = "agent-run-output:"
    if not artifact.reference.startswith(prefix):
        raise WorkspaceProjectionConflictError(
            "Artifact content is unavailable because its reference is not an AgentRun output."
        )
    output_id = artifact.reference.removeprefix(prefix)
    if not output_id:
        raise WorkspaceProjectionConflictError("Artifact AgentRun output reference is empty.")
    return output_id
