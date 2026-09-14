from __future__ import annotations

from app.agent.models import AgentRunCreateRequest
from app.agent.service import AgentRunService
from app.context.models import ContextBuildRequest
from app.execution import ExecutionFence
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import KnowledgeJob
from app.research_commands.models import (
    ProjectRunCommand,
    QuickReportCommand,
    ResearchCommand,
    ResearchCommandSubmission,
)
from app.research_commands.repository import ResearchCommandRepository


class ResearchCommandService:
    """Turn one durable, idempotent user command into one governed target."""

    def __init__(
        self,
        knowledge_repository: KnowledgeRepository,
        *,
        report_service: KnowledgeReportService,
        agent_service: AgentRunService,
        repository: ResearchCommandRepository | None = None,
    ) -> None:
        self.knowledge_repository = knowledge_repository
        self.report_service = report_service
        self.agent_service = agent_service
        self.repository = repository or ResearchCommandRepository(knowledge_repository)

    def submit(
        self,
        submission: ResearchCommandSubmission,
        *,
        idempotency_key: str,
    ) -> ResearchCommand:
        payload = submission.root.model_dump(mode="json")
        command, _ = self.repository.create_or_get(
            idempotency_key=idempotency_key,
            normalized_request=payload,
        )
        return self.refresh(command.id)

    def get(self, command_id: str) -> ResearchCommand:
        return self.refresh(command_id)

    def refresh_for_target(self, resource_type: str, resource_id: str) -> None:
        command_id = self.repository.find_id_by_target(resource_type, resource_id)
        if command_id is not None:
            self.refresh(command_id)

    def execute_claimed(self, job: KnowledgeJob) -> ResearchCommand:
        if job.kind != "research_command" or not job.lease_owner:
            raise ValueError("ResearchCommand execution requires a claimed command job.")
        fence = ExecutionFence.from_job(job)
        command = self.repository.mark_preparing(job.resource_id, fence)
        payload = ResearchCommandSubmission.model_validate(
            self.repository.request_payload(command.id)
        ).root
        try:
            if isinstance(payload, QuickReportCommand):
                return self._create_quick_report(command, payload, fence)
            return self._create_project_run(command, payload, fence)
        except Exception as exc:
            self.repository.mark_failed(command.id, fence=fence, error=str(exc))
            raise

    def retry(self, command_id: str) -> ResearchCommand:
        command = self.refresh(command_id)
        if command.status != "failed":
            raise ValueError("Only a failed research command can be retried.")
        if command.target_resource_type == "report" and command.target_resource_id:
            self.report_service.ensure_execution_available()
            report = self.knowledge_repository.reports.reset_report_for_retry(
                command.target_resource_id,
                auto_execute=True,
            )
            return self.repository.reflect_target(
                command.id,
                status="queued",
                orchestration_stage=report.status,
            )
        if command.target_resource_type == "agent_run" and command.target_resource_id:
            run = self.agent_service.resume_run(command.target_resource_id)
            return self.repository.reflect_target(
                command.id,
                status="queued",
                orchestration_stage=run.status,
            )
        return self.repository.retry_orchestration(command.id)

    def refresh(self, command_id: str) -> ResearchCommand:
        command = self.repository.get(command_id)
        if command.status not in {"queued", "completed", "failed"}:
            return command
        if command.target_resource_type == "report" and command.target_resource_id:
            report = self.knowledge_repository.reports.get_report(command.target_resource_id)
            if report.status == "completed":
                return self.repository.reflect_target(
                    command.id, status="completed", orchestration_stage="completed"
                )
            if report.status == "failed":
                return self.repository.reflect_target(
                    command.id,
                    status="failed",
                    orchestration_stage="target_failed",
                    error=report.error,
                )
            if command.status != "failed":
                return self.repository.reflect_target(
                    command.id, status="queued", orchestration_stage=report.status
                )
        if command.target_resource_type == "agent_run" and command.target_resource_id:
            run = self.agent_service.get_run(command.target_resource_id)
            if run.status in {"completed", "needs_review", "cancelled"}:
                return self.repository.reflect_target(
                    command.id,
                    status="completed",
                    orchestration_stage=run.status,
                )
            if run.status in {"failed", "stale_context"}:
                return self.repository.reflect_target(
                    command.id,
                    status="failed",
                    orchestration_stage=run.status,
                    error=run.error_message,
                )
            if command.status != "failed":
                return self.repository.reflect_target(
                    command.id, status="queued", orchestration_stage=run.status
                )
        return command

    def _create_quick_report(
        self,
        command: ResearchCommand,
        payload: QuickReportCommand,
        fence: ExecutionFence,
    ) -> ResearchCommand:
        report_id = _target_id("report", command.id)
        try:
            report = self.knowledge_repository.reports.get_report(report_id)
        except KeyError:
            report = self.report_service.submit(
                query=payload.instruction,
                topic_slugs=payload.collection_slugs,
                top_k=payload.top_k,
                report_depth=payload.report_depth,
                auto_execute=True,
                report_id=report_id,
            )
        self.repository.update_progress(
            command.id,
            fence=fence,
            status="target_created",
            orchestration_stage="target_created",
            target_resource_type="report",
            target_resource_id=report.id,
            target_route=f"/reports?report={report.id}",
        )
        return self.repository.update_progress(
            command.id,
            fence=fence,
            status="queued",
            orchestration_stage=report.status,
            target_resource_type="report",
            target_resource_id=report.id,
            target_route=f"/reports?report={report.id}",
        )

    def _create_project_run(
        self,
        command: ResearchCommand,
        payload: ProjectRunCommand,
        fence: ExecutionFence,
    ) -> ResearchCommand:
        memory = self.knowledge_repository.memory_repository
        memory.get_project(payload.project_id)
        if payload.task.kind == "existing":
            task = memory.get_workspace_task(payload.task.task_id)
            if task.project_id != payload.project_id:
                raise ValueError("WorkspaceTask does not belong to the selected Project.")
        else:
            task_id = command.task_id or _target_id("workspace-task", command.id)
            try:
                task = memory.get_workspace_task(task_id)
            except KeyError:
                title = (payload.task.title or payload.instruction[:60]).strip()
                task = memory.create_workspace_task(
                    project_id=payload.project_id,
                    title=title,
                    goal=payload.instruction,
                    priority=payload.task.priority,
                    metadata={"research_command_id": command.id},
                    task_id=task_id,
                )
        command = self.repository.update_progress(
            command.id,
            fence=fence,
            status="preparing",
            orchestration_stage="task_ready",
            task_id=task.id,
        )
        snapshot_id = command.snapshot_id or _target_id("context-snapshot", command.id)
        try:
            context = self.agent_service.context_builder.snapshot_repository.get(snapshot_id)
        except KeyError:
            context = self.agent_service.context_builder.build_context(
                ContextBuildRequest(
                    task_id=task.id,
                    project_id=payload.project_id,
                    snapshot_id=snapshot_id,
                    max_tokens=payload.token_budget,
                )
            )
        command = self.repository.update_progress(
            command.id,
            fence=fence,
            status="preparing",
            orchestration_stage="snapshot_ready",
            task_id=task.id,
            snapshot_id=context.snapshot_id,
        )
        run_id = command.target_resource_id or _target_id("agent-run", command.id)
        try:
            run = self.agent_service.get_run(run_id)
        except KeyError:
            run = self.agent_service.create_run(
                payload.project_id,
                task.id,
                AgentRunCreateRequest(
                    context_snapshot_id=context.snapshot_id,
                    workflow="research",
                    create_memory_proposal=payload.create_memory_proposal,
                    max_steps=payload.max_steps,
                    max_tool_calls=payload.max_tool_calls,
                    token_budget=payload.token_budget,
                ),
                run_id=run_id,
            )
        self.repository.update_progress(
            command.id,
            fence=fence,
            status="target_created",
            orchestration_stage="target_created",
            task_id=task.id,
            snapshot_id=context.snapshot_id,
            target_resource_type="agent_run",
            target_resource_id=run.id,
            target_route=f"/agent-runs/{run.id}",
        )
        return self.repository.update_progress(
            command.id,
            fence=fence,
            status="queued",
            orchestration_stage=run.status,
            task_id=task.id,
            snapshot_id=context.snapshot_id,
            target_resource_type="agent_run",
            target_resource_id=run.id,
            target_route=f"/agent-runs/{run.id}",
        )


def _target_id(prefix: str, command_id: str) -> str:
    return f"{prefix}-command-{command_id.removeprefix('research-command-')}"
