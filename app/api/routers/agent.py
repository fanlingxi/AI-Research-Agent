from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.agent.feedback_models import (
    FeedbackDecision,
    FeedbackExportRequest,
    FeedbackRecheck,
    FeedbackRequest,
    FeedbackRerunRequest,
)
from app.agent.models import AgentRunCreateRequest, AgentRunReviewRequest
from app.agent.service import AgentRunService
from app.api.errors import agent_call, memory_call, workspace_call
from app.memory.service import MemoryService
from app.workspace.schemas import ContextProjectionRequest
from app.workspace.service import WorkspaceProjectionService


def build_agent_router(
    memory: MemoryService,
    agents: AgentRunService,
    workspace: WorkspaceProjectionService,
) -> APIRouter:
    router = APIRouter(tags=["agent"])

    @router.post(
        "/api/projects/{project_id}/workspace-tasks/{task_id}/agent-runs",
        status_code=202,
    )
    def create_agent_run(
        project_id: str, task_id: str, payload: AgentRunCreateRequest
    ) -> dict[str, Any]:
        memory_call(lambda: memory.repository.get_project(project_id))
        return agent_call(lambda: agents.create_run(project_id, task_id, payload)).model_dump()

    @router.get("/api/v1/projects/{project_id}/workspace-tasks/{task_id}/agent-runs")
    def list_task_agent_runs(
        project_id: str,
        task_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.list_task_runs(
                project_id, task_id, limit=limit, cursor=cursor
            )
        )

    @router.get("/api/v1/projects/{project_id}/agent-runs")
    def list_project_agent_runs(
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.list_project_runs(project_id, limit=limit, cursor=cursor)
        )

    @router.post(
        "/api/v1/projects/{project_id}/workspace-tasks/{task_id}/context-snapshots/preview"
    )
    def preview_workspace_task_context(
        project_id: str, task_id: str, payload: ContextProjectionRequest
    ) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.preview_context(project_id, task_id, payload)
        ).model_dump()

    @router.post(
        "/api/v1/projects/{project_id}/workspace-tasks/{task_id}/context-snapshots",
        status_code=201,
    )
    def create_workspace_task_context_snapshot(
        project_id: str, task_id: str, payload: ContextProjectionRequest
    ) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.create_context_snapshot(project_id, task_id, payload)
        ).model_dump()

    @router.get("/api/v1/context-snapshots/{snapshot_id}")
    def get_context_snapshot_summary(snapshot_id: str) -> dict[str, Any]:
        return workspace_call(lambda: workspace.get_context_snapshot(snapshot_id)).model_dump()

    @router.get("/api/v1/context-snapshots/{snapshot_id}/items")
    def list_context_snapshot_items(
        snapshot_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.list_context_snapshot_items(
                snapshot_id, offset=offset, limit=limit
            )
        ).model_dump()

    @router.get("/api/v1/context-snapshots/{snapshot_id}/evidence/{evidence_id}")
    def get_context_snapshot_evidence(snapshot_id: str, evidence_id: str) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.get_snapshot_evidence(snapshot_id, evidence_id)
        ).model_dump()

    @router.get("/api/agent-runs/{run_id}")
    def get_agent_run(run_id: str) -> dict[str, Any]:
        return agent_call(lambda: agents.get_run(run_id)).model_dump()

    @router.get("/api/agent-runs/{run_id}/events")
    def list_agent_run_events(run_id: str) -> list[dict[str, Any]]:
        events = agent_call(lambda: agents.repository.list_events(run_id))
        return [item.model_dump() for item in events]

    @router.get("/api/agent-runs/{run_id}/tool-calls")
    def list_agent_tool_calls(run_id: str) -> list[dict[str, Any]]:
        tool_calls = agent_call(lambda: agents.repository.list_tool_calls(run_id))
        return [item.model_dump() for item in tool_calls]

    @router.get("/api/v1/agent-runs/{run_id}/trace")
    def get_agent_run_trace(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return workspace_call(
            lambda: workspace.get_run_trace(
                run_id, after_sequence=after_sequence, limit=limit
            )
        ).model_dump()

    @router.get("/api/agent-runs/{run_id}/output")
    def get_agent_run_output(run_id: str) -> dict[str, Any]:
        return agent_call(lambda: agents.repository.get_output(run_id)).model_dump()

    @router.post("/api/agent-runs/{run_id}/cancel")
    def cancel_agent_run(run_id: str) -> dict[str, Any]:
        return agent_call(lambda: agents.cancel_run(run_id)).model_dump()

    @router.post("/api/agent-runs/{run_id}/resume", status_code=202)
    def resume_agent_run(run_id: str) -> dict[str, Any]:
        return agent_call(lambda: agents.resume_run(run_id)).model_dump()

    @router.post("/api/agent-runs/{run_id}/review", status_code=202)
    def review_agent_run(run_id: str, payload: AgentRunReviewRequest) -> dict[str, Any]:
        return agent_call(
            lambda: agents.review_run(run_id, action=payload.action)
        ).model_dump()

    @router.get("/api/agent-runs/{run_id}/feedback")
    def list_feedback(run_id: str) -> dict[str, Any]:
        return agent_call(lambda: agents.feedback.list(run_id))

    @router.post("/api/agent-runs/{run_id}/feedback", status_code=201)
    def create_feedback(run_id: str, payload: FeedbackRequest) -> dict[str, Any]:
        return agent_call(lambda: agents.feedback.create(run_id, payload))

    @router.post("/api/agent-runs/{run_id}/feedback/{feedback_id}/review")
    def review_feedback(
        run_id: str, feedback_id: str, payload: FeedbackDecision
    ) -> dict[str, Any]:
        return agent_call(lambda: agents.feedback.decide(run_id, feedback_id, payload))

    @router.post("/api/agent-runs/{run_id}/feedback/{feedback_id}/rerun", status_code=202)
    def rerun_feedback(
        run_id: str, feedback_id: str, payload: FeedbackRerunRequest
    ) -> dict[str, Any]:
        return agent_call(lambda: agents.feedback.rerun(run_id, feedback_id, payload)).model_dump()

    @router.post("/api/agent-runs/{run_id}/rechecks/{recheck_id}")
    def recheck_feedback(
        run_id: str, recheck_id: str, payload: FeedbackRecheck
    ) -> dict[str, Any]:
        return agent_call(lambda: agents.feedback.recheck(run_id, recheck_id, payload))

    @router.post("/api/agent-runs/{run_id}/feedback/{feedback_id}/dev-candidate")
    def export_feedback(
        run_id: str, feedback_id: str, payload: FeedbackExportRequest
    ) -> dict[str, Any]:
        return agent_call(lambda: agents.feedback.export_candidate(
            run_id, feedback_id, payload.target_dev_version
        ))

    return router
