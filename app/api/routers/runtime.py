from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query

from app.agent.errors import AgentRunConflictError
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.rebuild_jobs import ProjectionRebuildJobs, RebuildRequest
from app.research_commands.models import ResearchCommandConflictError, ResearchCommandSubmission
from app.research_commands.service import ResearchCommandService
from app.runtime.service import RuntimeObservabilityService


def build_runtime_router(
    runtime: RuntimeObservabilityService,
    research_commands: ResearchCommandService,
) -> APIRouter:
    router = APIRouter(tags=["runtime"])
    rebuilds = ProjectionRebuildJobs(runtime.repository)

    def require_current_worker():
        for worker in runtime.repository.jobs.list_executor_heartbeats():
            if (
                worker.role == "worker"
                and worker.version != "unified-worker-v2"
                and (
                    datetime.now(UTC) - datetime.fromisoformat(worker.last_heartbeat_at)
                ).total_seconds()
                < 180
            ):
                raise HTTPException(409, "请先停止旧版 Worker 并使用当前版本重启。")

    @router.post("/api/v1/runtime/rebuilds", status_code=202)
    def submit_rebuild(
        payload: RebuildRequest,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
        ],
    ) -> dict[str, Any]:
        require_current_worker()
        try:
            return rebuilds.submit(payload, idempotency_key).model_dump()
        except KeyError as exc:
            raise HTTPException(404, "Collection 不存在。") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/api/v1/runtime/rebuilds/{job_id}")
    def get_rebuild(job_id: str) -> dict[str, Any]:
        try:
            return rebuilds.get(job_id).model_dump()
        except KeyError as exc:
            raise HTTPException(404, "投影重建不存在。") from exc

    @router.post("/api/v1/runtime/rebuilds/{job_id}/retry", status_code=202)
    def retry_rebuild(job_id: str) -> dict[str, Any]:
        require_current_worker()
        try:
            return rebuilds.retry(job_id).model_dump()
        except KeyError as exc:
            raise HTTPException(404, "投影重建或Collection不存在。") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/api/v1/runtime/overview")
    def runtime_overview() -> dict[str, Any]:
        return runtime.overview().model_dump()

    @router.get("/api/v1/runtime/work")
    def runtime_work(
        kind: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        try:
            return runtime.list_work(
                kind=kind, status=status, cursor=cursor, limit=limit
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/v1/runtime/projections/{event_id}/retry")
    def retry_runtime_projection(event_id: str) -> dict[str, Any]:
        try:
            return runtime.retry_projection(event_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Projection event not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v1/research-commands", status_code=202)
    def create_research_command(
        payload: ResearchCommandSubmission,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=8, max_length=200),
        ],
    ) -> dict[str, Any]:
        try:
            return research_commands.submit(payload, idempotency_key=idempotency_key).model_dump()
        except ResearchCommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/v1/research-commands/{command_id}")
    def get_research_command(command_id: str) -> dict[str, Any]:
        try:
            return research_commands.get(command_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Research command not found.") from exc

    @router.post("/api/v1/research-commands/{command_id}/retry", status_code=202)
    def retry_research_command(command_id: str) -> dict[str, Any]:
        try:
            return research_commands.retry(command_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Research command not found.") from exc
        except (ValueError, LiveLLMRequiredError, AgentRunConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
