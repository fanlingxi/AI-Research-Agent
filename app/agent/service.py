"""Application service for AgentRun lifecycle and durable queue coordination."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from app.agent.errors import AgentRunConflictError
from app.agent.feedback import RunFeedbackService
from app.agent.feedback_models import FeedbackDecision, FeedbackRequest, FeedbackRerunRequest
from app.agent.models import (
    AgentRun,
    AgentRunCreateRequest,
    AgentRunOptions,
    AgentRunOutput,
    AgentToolCall,
    DomainFinalization,
    ResearchFinalization,
)
from app.agent.repository import AgentRunRepository
from app.config.settings import Settings, get_settings
from app.context.models import ContextBuildRequest, ContextPackage
from app.context.service import ContextBuilderService
from app.domain_plugins.contracts import DomainFinalizationCommand
from app.domain_plugins.errors import DomainPluginConflictError
from app.domain_plugins.ports import DomainRuntimePort
from app.domain_plugins.registry import DomainPluginRegistry, create_builtin_plugin_registry
from app.domain_plugins.research.ports import ResearchRuntimePort
from app.execution import ExecutionFence
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import LLMClient, MockLLMClient, get_llm_client
from app.memory.models import MemoryProposal
from app.retrieval.query_planning import QueryPlanner
from app.retrieval.reranking import EvidenceReranker

FOUNDATION_WORKFLOW_NAME = "research_agent_foundation"
FOUNDATION_WORKFLOW_VERSION = "phase3a-v1"
RESEARCH_WORKFLOW_NAME = "research_agent"
RESEARCH_WORKFLOW_VERSION = "phase3b-v1"


class AgentRunService:
    """Coordinate Context input, AgentRun audit facts, and the existing queue.

    Graph nodes call this service only; they never access SQLite directly.
    """

    def __init__(
        self,
        knowledge_repository: KnowledgeRepository,
        *,
        context_builder: ContextBuilderService | None = None,
        repository: AgentRunRepository | None = None,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        plugin_registry: DomainPluginRegistry | None = None,
    ) -> None:
        self.knowledge_repository = knowledge_repository
        self.repository = repository or AgentRunRepository(
            knowledge_repository.path, knowledge_repository.database
        )
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_client(self.settings)
        self.context_builder = context_builder or ContextBuilderService(
            knowledge_repository, query_planner=QueryPlanner(self.llm),
            evidence_reranker=EvidenceReranker(self.llm)
        )
        self.plugin_registry = plugin_registry or create_builtin_plugin_registry()
        self.feedback = RunFeedbackService(self)
        self._current_execution: ContextVar[ExecutionFence | None] = ContextVar(
            f"agent_execution_fence_{id(self)}",
            default=None,
        )

    @contextmanager
    def execution_scope(self, fence: ExecutionFence | None) -> Iterator[None]:
        token = self._current_execution.set(fence)
        try:
            yield
        finally:
            self._current_execution.reset(token)

    def create_run(
        self,
        project_id: str,
        task_id: str,
        request: AgentRunCreateRequest,
        *,
        run_id: str | None = None,
    ) -> AgentRun:
        return self._create_run(project_id, task_id, request, run_id=run_id)

    def _create_run(
        self,
        project_id: str,
        task_id: str,
        request: AgentRunCreateRequest,
        *,
        run_id: str | None = None,
        feedback_rerun: tuple[str, str, FeedbackRerunRequest, int] | None = None,
    ) -> AgentRun:
        task = self.knowledge_repository.memory_repository.get_workspace_task(task_id)
        if task.project_id != project_id:
            raise AgentRunConflictError("WorkspaceTask does not belong to the requested Project.")
        binding = self.knowledge_repository.memory_repository.get_project_domain_plugin(
            project_id, task.domain_plugin_key
        )
        if binding.status != "enabled":
            raise DomainPluginConflictError(
                f"Domain Plugin {task.domain_plugin_key} is not enabled for this Project."
            )
        plugin = self.plugin_registry.get(task.domain_plugin_key)
        workflow_key = request.workflow or plugin.manifest.default_workflow_key
        plugin.validate_run_request(
            workflow_key,
            max_steps=request.max_steps,
            max_tool_calls=request.max_tool_calls,
            is_mock_llm=self._is_mock_llm(),
        )
        pin = self.plugin_registry.pin(task.domain_plugin_key, workflow_key)
        workflow = plugin.workflow_spec(workflow_key)
        context = self._resolve_context(project_id, task_id, request)
        provider, model = self._model_metadata()
        options = AgentRunOptions(
            workflow=workflow_key,
            create_memory_proposal=request.create_memory_proposal,
        )
        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if feedback_rerun:
                parent_id, feedback_id, rerun_request, parent_revision = feedback_rerun
                duplicate = self.feedback.check_rerun_tx(
                    connection, parent_id, feedback_id, rerun_request
                )
                if duplicate:
                    return duplicate
                parent = self.repository._require_run_tx(connection, parent_id)
                if (
                    parent["revision"] != parent_revision
                    or parent["plugin_key"] != pin.key
                    or parent["project_id"] != project_id
                    or parent["task_id"] != task_id
                    or parent["context_snapshot_id"] == context.snapshot_id
                ):
                    raise AgentRunConflictError("Review origin changed during context preparation.")
                self.context_builder.validate_fresh_context_tx(connection, context)
                if (pin.key == "research" and request.workflow == "research"
                        and not context.project.knowledge_scopes):
                    raise AgentRunConflictError("Research rerun requires an explicit scope.")
            try:
                self.knowledge_repository.memory_repository.require_workspace_task_domain_plugin_tx(
                    connection,
                    project_id=project_id,
                    task_id=task_id,
                    plugin_key=pin.key,
                )
            except ValueError as exc:
                raise AgentRunConflictError(str(exc)) from exc
            run = self.repository.create_run_tx(
                connection,
                project_id=project_id,
                task_id=task_id,
                context_snapshot_id=context.snapshot_id,
                context_sha256=context.package_sha256,
                workflow_name=workflow.name,
                workflow_version=workflow.version,
                plugin=pin,
                options=options,
                model_provider=provider,
                model_name=model,
                max_steps=request.max_steps,
                max_tool_calls=request.max_tool_calls,
                token_budget=request.token_budget,
                run_id=run_id,
            )
            queued = self.repository.queue_run_tx(connection, run.id)
            self.knowledge_repository.jobs.enqueue_job_tx(
                connection,
                kind="agent_run",
                resource_id=run.id,
                payload={
                    "workflow_name": run.workflow_name,
                    "plugin": run.plugin.model_dump(mode="json"),
                },
                priority=100,
            )
            if feedback_rerun:
                self.feedback.link_rerun_tx(
                    connection, parent_id, feedback_id, rerun_request, queued
                )
            return queued

    def cancel_run(self, run_id: str) -> AgentRun:
        run = self.repository.cancel_run(run_id)
        # A claimed worker still checks the cancelled business status before it
        # invokes a graph. Completing a queued job avoids unnecessary work.
        self.knowledge_repository.jobs.complete_resource_job("agent_run", run_id)
        return run

    def resume_run(self, run_id: str) -> AgentRun:
        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            resumed = self.repository.resume_run_tx(connection, run_id)
            self.knowledge_repository.jobs.enqueue_job_tx(
                connection,
                kind="agent_run",
                resource_id=run_id,
                payload={"workflow_name": resumed.workflow_name},
                priority=100,
                force_requeue=True,
            )
            return resumed

    def review_run(self, run_id: str, *, action: str) -> AgentRun:
        """Close an invalid run or start a new run from a fresh ContextSnapshot."""

        reviewed = self.repository.get_run(run_id)
        # Legacy rerun requests use the same governed path and a stable key.
        # Repeated delivery returns the existing child even after it completed.
        if action == "rerun":
            existing = self.feedback.list(run_id)
            for item in existing["feedback"]:
                if item["request"]["idempotency_key"] == "legacy-review-rerun":
                    if item["status"] == "pending":
                        self.feedback.decide(run_id, item["id"], FeedbackDecision(
                            expected_revision=1, decision="accepted",
                            reviewer="legacy review endpoint",
                            note=("Operator requested a new run; "
                                  "this accepts the issue, not the draft."),
                        ))
                    return self.feedback.rerun(
                        run_id, item["id"], FeedbackRerunRequest(
                            idempotency_key="legacy-review-rerun", expected_revision=2,
                            resolution_note="Explicit rerun from the existing review endpoint.",
                        ),
                    )
        if reviewed.status != "needs_review":
            raise AgentRunConflictError("Only needs_review AgentRuns have review actions.")
        if action == "close":
            return self.repository.close_needs_review(run_id)
        if action != "rerun":
            raise AgentRunConflictError(f"Unsupported review action: {action}")
        feedback = self.feedback.create(run_id, FeedbackRequest(
            idempotency_key="legacy-review-rerun", category="citation",
            reporter="legacy review endpoint",
            note=reviewed.error_message or "Citation validation requires review.",
        ))
        self.feedback.decide(run_id, feedback["id"], FeedbackDecision(
            expected_revision=1, decision="accepted", reviewer="legacy review endpoint",
            note="Operator requested a new run; this accepts the issue, not the draft.",
        ))
        return self.feedback.rerun(
            run_id, feedback["id"], FeedbackRerunRequest(
                idempotency_key="legacy-review-rerun", expected_revision=2,
                resolution_note="Explicit rerun from the existing review endpoint.",
            ),
        )

    def get_run(self, run_id: str) -> AgentRun:
        return self.repository.get_run(run_id)

    def domain_runtime_port(self) -> DomainRuntimePort:
        """Return the only Runtime surface supplied to a Domain Plugin."""

        return DomainRuntimePort(self)

    def research_runtime_port(self) -> ResearchRuntimePort:
        """Return the explicit Phase 3 compatibility surface for Research only."""

        return ResearchRuntimePort(self)

    def runtime_port_for(self, port_kind: str) -> DomainRuntimePort:
        """Map a reviewed manifest capability to its bounded Runtime port."""

        if port_kind == "research":
            return self.research_runtime_port()
        if port_kind == "generic":
            return self.domain_runtime_port()
        raise AgentRunConflictError(f"Unknown Domain Runtime port kind: {port_kind}")

    def recover_interrupted_run(self, run_id: str) -> AgentRun:
        return self.repository.recover_interrupted_run(
            run_id,
            execution_fence=self._current_execution.get(),
        )

    def mark_stale_context_if_needed(self, run_id: str) -> AgentRun:
        return self.repository.mark_stale_context_if_needed(
            run_id,
            execution_fence=self._current_execution.get(),
        )

    def mark_node(
        self, run_id: str, *, status: str, node_name: str, input_summary: dict[str, Any]
    ) -> AgentRun:
        return self.repository.mark_node(
            run_id,
            status=status,
            node_name=node_name,
            input_summary=input_summary,
            execution_fence=self._current_execution.get(),
        )

    def record_node_completed(
        self,
        run_id: str,
        *,
        node_name: str,
        output_summary: dict[str, Any],
        token_usage: dict[str, Any] | None = None,
        latency_ms: float | None = None,
    ) -> None:
        self.repository.record_node_completed(
            run_id,
            node_name=node_name,
            output_summary=output_summary,
            token_usage=token_usage,
            latency_ms=latency_ms,
            execution_fence=self._current_execution.get(),
        )

    def record_tool_call(
        self,
        run_id: str,
        *,
        sequence: int,
        tool_name: str,
        permission: str,
        arguments: dict[str, Any],
        result_summary: dict[str, Any],
        idempotency_key: str,
        node_name: str = "inspect_context",
    ) -> AgentToolCall:
        run = self.get_run(run_id)
        plugin = self.plugin_registry.resolve_pin(run.plugin)
        if permission not in plugin.manifest.allowed_permissions:
            raise AgentRunConflictError(
                f"Domain Plugin {run.plugin.key} does not permit {permission} tool calls."
            )
        return self.repository.record_tool_call(
            run_id,
            sequence=sequence,
            tool_name=tool_name,
            permission=permission,
            arguments=arguments,
            result_summary=result_summary,
            idempotency_key=idempotency_key,
            node_name=node_name,
            execution_fence=self._current_execution.get(),
        )

    def complete_foundation_output(
        self, run_id: str, *, tool_result: dict[str, Any]
    ) -> AgentRunOutput:
        structured = {
            "kind": "phase3a_foundation_placeholder",
            "run_id": run_id,
            "tool_result": tool_result,
        }
        return self.repository.complete_with_output(
            run_id,
            output_type="agent_runtime_foundation",
            structured=structured,
            rendered_text=(
                "Phase 3A runtime foundation completed; no research content was generated."
            ),
            validation={"status": "not_applicable", "phase": "3a"},
            execution_fence=self._current_execution.get(),
        )

    def begin_repair(self, run_id: str) -> AgentRun:
        return self.repository.begin_repair(
            run_id,
            execution_fence=self._current_execution.get(),
        )

    def begin_generation_attempt(self, run_id, slot, digest):
        from app.agent.generation_attempts import begin
        return begin(self, run_id, slot, digest)

    def finish_generation_attempt(self, run_id, slot, **kwargs):
        from app.agent.generation_attempts import finish
        return finish(self, run_id, slot, **kwargs)

    def mark_needs_review(self, run_id: str, *, error_message: str) -> AgentRun:
        return self.repository.mark_needs_review(
            run_id,
            error_code="citation_validation_failed",
            error_message=error_message,
            execution_fence=self._current_execution.get(),
        )

    def load_context_snapshot(self, run_id: str) -> ContextPackage:
        """Load the immutable, hash-checked input attached to an AgentRun."""

        run = self.get_run(run_id)
        package = self.context_builder.snapshot_repository.get(run.context_snapshot_id)
        if package.package_sha256 != run.context_sha256:
            raise AgentRunConflictError("ContextSnapshot hash no longer matches AgentRun input.")
        return package

    def finalize_run(
        self,
        run_id: str,
        *,
        command: DomainFinalizationCommand,
    ) -> DomainFinalization:
        """Atomically persist one validated plugin output and its governed records."""

        with self.knowledge_repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fence = self._current_execution.get()
            if fence is not None:
                self.knowledge_repository.jobs.assert_job_ownership_tx(
                    connection,
                    job_id=fence.job_id,
                    kind="agent_run",
                    resource_id=run_id,
                    expected_attempt=fence.attempt,
                    expected_owner=fence.owner_id,
                )
            run = self.repository.get_run_tx(connection, run_id)
            if command.plugin != run.plugin:
                raise AgentRunConflictError(
                    "Domain finalization PluginPin does not match AgentRun."
                )
            plugin = self.plugin_registry.resolve_pin(run.plugin)
            if command.artifact_type not in plugin.manifest.artifact_types:
                raise AgentRunConflictError(
                    f"Domain Plugin {run.plugin.key} cannot create "
                    f"{command.artifact_type} Artifacts."
                )
            if (
                command.memory_proposal_payload is not None
                and not plugin.manifest.supports_memory_proposal
            ):
                raise AgentRunConflictError(
                    f"Domain Plugin {run.plugin.key} cannot create MemoryProposals."
                )
            output_id = f"agent-output-{uuid4().hex}"
            artifact = self.knowledge_repository.memory_repository.create_artifact_tx(
                connection,
                project_id=run.project_id,
                task_id=run.task_id,
                artifact_type=command.artifact_type,
                reference=f"agent-run-output:{output_id}",
                status="ready",
                metadata={
                    **command.artifact_metadata,
                    "agent_run_id": run_id,
                    "context_snapshot_id": run.context_snapshot_id,
                    "context_sha256": run.context_sha256,
                    "agent_output_id": output_id,
                    "plugin": run.plugin.model_dump(mode="json"),
                    "validation": command.validation,
                },
            )
            proposal: MemoryProposal | None = None
            if run.options.create_memory_proposal and command.memory_proposal_payload is not None:
                proposal = self.knowledge_repository.memory_repository.create_proposal_tx(
                    connection,
                    project_id=run.project_id,
                    task_id=run.task_id,
                    payload=command.memory_proposal_payload,
                    rationale=str(
                        command.memory_proposal_payload.get("rationale")
                        or f"{plugin.manifest.display_name} Agent proposal."
                    ),
                )
            structured = {
                **command.structured_output,
                "context_snapshot_id": run.context_snapshot_id,
                "context_sha256": run.context_sha256,
                "artifact_id": artifact.id,
                "memory_proposal_id": proposal.id if proposal else None,
                "plugin": run.plugin.model_dump(mode="json"),
            }
            output = self.repository.complete_with_output_tx(
                connection,
                run_id,
                output_type=command.output_type,
                structured=structured,
                rendered_text=command.rendered_text,
                validation=command.validation,
                output_id=output_id,
                completion_summary={
                    "artifact_id": artifact.id,
                    "memory_proposal_id": proposal.id if proposal else None,
                },
                execution_fence=fence,
            )
            return DomainFinalization(
                output=output,
                artifact=artifact,
                memory_proposal=proposal,
            )

    def finalize_research_run(
        self,
        run_id: str,
        *,
        draft: dict[str, Any],
        validation: dict[str, Any],
        memory_proposal_payload: dict[str, Any] | None,
    ) -> ResearchFinalization:
        """Compatibility wrapper for the frozen Phase 3 Research service API."""

        run = self.get_run(run_id)
        if run.plugin.key != "research":
            raise AgentRunConflictError("Only the Research Plugin can use this compatibility API.")
        finalized = self.finalize_run(
            run_id,
            command=DomainFinalizationCommand(
                plugin=run.plugin,
                output_type="research_report",
                structured_output={"kind": "research_report", "draft": draft},
                rendered_text=str(draft["markdown"]),
                validation=validation,
                artifact_type="research_report",
                memory_proposal_payload=memory_proposal_payload,
            ),
        )
        return ResearchFinalization.model_validate(finalized.model_dump(mode="json"))

    def fail_run(self, run_id: str, *, error_code: str, error_message: str) -> AgentRun:
        return self.repository.fail_run(
            run_id,
            error_code=error_code,
            error_message=error_message,
            execution_fence=self._current_execution.get(),
        )

    def _resolve_context(
        self, project_id: str, task_id: str, request: AgentRunCreateRequest
    ) -> ContextPackage:
        if request.context_snapshot_id:
            context = self.context_builder.snapshot_repository.get(request.context_snapshot_id)
        else:
            context = self.context_builder.build_context(
                ContextBuildRequest(
                    task_id=task_id,
                    project_id=project_id,
                    # A run spans multiple model calls; snapshot capacity is separate.
                    query_planning=request.query_planning,
                    context_neighbors=request.context_neighbors,
                    evidence_reranking=request.evidence_reranking,
                    reading_format=(request.reading_format if request.reading_format != 'legacy'
                                    else 'research-v1' if request.workflow in {
                                        'research_v2', 'research_v3', 'research_v4',
                                        'research_v5', 'research_v6', 'research_v7'
                                    }
                                    else 'legacy'),
                    max_tokens=(request.context_max_tokens if request.context_max_tokens is not None
                                else min(request.token_budget, 16000)),
                )
            )
        if context.project.project_id != project_id or context.task.task_id != task_id:
            raise AgentRunConflictError(
                "ContextSnapshot does not belong to the requested Project and Task."
            )
        if request.workflow in {
            'research_v2', 'research_v3', 'research_v4', 'research_v5', 'research_v6', 'research_v7'
        }:
            if context.reading_format == 'legacy':
                raise AgentRunConflictError(
                    f'{request.workflow} requires a new versioned reading snapshot'
                )
            if not context.project.knowledge_scopes:
                raise AgentRunConflictError(
                    f'{request.workflow} requires an explicit knowledge scope'
                )
        return context

    def _model_metadata(self) -> tuple[str, str]:
        provider = self.settings.llm_provider
        model = {
            "openai": self.settings.llm_model,
            "qwen": self.settings.qwen_model,
            "deepseek": self.settings.deepseek_model,
        }.get(provider, "mock")
        return provider, model

    def _is_mock_llm(self) -> bool:
        return isinstance(self.llm, MockLLMClient) or (
            getattr(self.llm, "provider_name", "mock") == "mock"
        )
