from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

from app.agent.errors import (
    AgentRunConflictError,
    AgentRunNotRecoverableError,
    AgentRunTerminalError,
    ResearchLLMRequiredError,
)
from app.agent.models import AgentRunCreateRequest
from app.agent.service import AgentRunService
from app.config.settings import get_settings
from app.context.repository import SnapshotIntegrityError
from app.context.service import ContextBudgetTooSmallError, ContextConflictError
from app.domain_plugins.errors import DomainPluginConflictError
from app.domain_plugins.game_modeling.knowledge import GameKnowledgeAuthoringService
from app.domain_plugins.game_modeling.models import (
    GameFormulaCandidateCreateRequest,
    GamePatchCandidateCreateRequest,
)
from app.domain_plugins.models import (
    ProjectDomainPluginUpdateRequest,
    WorkspaceTaskPluginBindRequest,
)
from app.domain_plugins.service import DomainPluginService
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import BulkCandidateDecision, CandidateDecision, CandidateStatus
from app.knowledge.service import KnowledgeIngestionService
from app.memory.schemas import (
    ArtifactCreateRequest,
    ArtifactNextVersionRequest,
    ArtifactStatusRequest,
    DecisionCreateRequest,
    DecisionStatusRequest,
    MemoryProposalCreateRequest,
    MemoryProposalReviewRequest,
    ProjectCreateRequest,
    ProjectKnowledgeScopeReplaceRequest,
    ProjectStatusRequest,
    ProjectUpdateRequest,
    RevisionRequest,
    WorkspaceTaskCreateRequest,
    WorkspaceTaskStatusRequest,
    WorkspaceTaskUpdateRequest,
)
from app.memory.service import MemoryService
from app.research_commands.models import (
    ResearchCommandConflictError,
    ResearchCommandSubmission,
)
from app.research_commands.service import ResearchCommandService
from app.runtime.service import RuntimeObservabilityService
from app.workspace.schemas import ContextProjectionRequest
from app.workspace.service import WorkspaceProjectionConflictError, WorkspaceProjectionService


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


def create_app(
    knowledge_repository: KnowledgeRepository | None = None,
    knowledge_service: KnowledgeIngestionService | None = None,
    report_service: KnowledgeReportService | None = None,
    memory_service: MemoryService | None = None,
    agent_run_service: AgentRunService | None = None,
    domain_plugin_service: DomainPluginService | None = None,
    game_knowledge_authoring_service: GameKnowledgeAuthoringService | None = None,
    workspace_projection_service: WorkspaceProjectionService | None = None,
    runtime_observability_service: RuntimeObservabilityService | None = None,
    research_command_service: ResearchCommandService | None = None,
) -> FastAPI:
    settings = get_settings()
    repository = knowledge_repository or KnowledgeRepository(settings.knowledge_db_path)
    service = knowledge_service or KnowledgeIngestionService(repository, settings=settings)
    reports = report_service or KnowledgeReportService(repository, settings=settings)
    memory = memory_service or MemoryService(repository.memory_repository)
    agents = agent_run_service or AgentRunService(repository)
    domain_plugins = domain_plugin_service or DomainPluginService(
        repository.memory_repository, agents.plugin_registry
    )
    game_knowledge = game_knowledge_authoring_service or GameKnowledgeAuthoringService(repository)
    workspace = workspace_projection_service or WorkspaceProjectionService(
        repository,
        context_builder=agents.context_builder,
        agent_run_service=agents,
    )
    runtime_observability = runtime_observability_service or RuntimeObservabilityService(
        repository,
        settings,
        llm_provider=getattr(service.llm, "provider_name", settings.llm_provider),
        live_llm_configured=not service._is_mock_llm(),
    )
    research_commands = research_command_service or ResearchCommandService(
        repository,
        report_service=reports,
        agent_service=agents,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.knowledge_repository = repository
        app.state.knowledge_service = service
        app.state.report_service = reports
        app.state.memory_service = memory
        app.state.agent_run_service = agents
        app.state.domain_plugin_service = domain_plugins
        app.state.game_knowledge_authoring_service = game_knowledge
        app.state.workspace_projection_service = workspace
        app.state.runtime_observability_service = runtime_observability
        app.state.research_command_service = research_commands
        yield

    app = FastAPI(
        title="AI Research Knowledge Core API",
        version="0.2.0",
        description="PDF-first, reviewable research knowledge and grounded reports.",
        lifespan=lifespan,
    )

    def health_payload() -> dict[str, Any]:
        worker_heartbeats = repository.list_executor_heartbeats("worker")
        worker = worker_heartbeats[0] if worker_heartbeats else None
        worker_available = False
        if worker is not None:
            last_seen = datetime.fromisoformat(worker.last_heartbeat_at)
            worker_available = (
                datetime.now(tz=UTC) - last_seen
            ).total_seconds() <= max(5.0, settings.knowledge_worker_poll_seconds * 3)
        return {
            "status": "ok",
            "services": {
                "qdrant": _service_status(settings.qdrant_url),
                "neo4j": _service_status(settings.neo4j_uri),
                "worker": {
                    "available": worker_available,
                    "detail": (
                        f"统一任务执行器在线 · {worker.id}"
                        if worker_available and worker is not None
                        else "统一任务执行器离线；请启动 python -m app.worker"
                    ),
                },
            },
            "knowledge": {
                "database": settings.knowledge_db_path,
                "schema_version": repository.schema_version(),
                "vault": settings.knowledge_vault_path,
                "llm_provider": getattr(service.llm, "provider_name", "mock"),
                "live_llm_configured": not service._is_mock_llm(),
                "jobs": repository.job_summary(),
                "projections": repository.projection_summary(),
            },
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Operational health endpoint used by Streamlit and service probes."""
        return health_payload()

    @app.get("/api/knowledge/health")
    def knowledge_health() -> dict[str, Any]:
        """Health endpoint available through the React development proxy."""
        return health_payload()

    @app.get("/api/v1/runtime/overview")
    def runtime_overview() -> dict[str, Any]:
        return runtime_observability.overview().model_dump()

    @app.get("/api/v1/runtime/work")
    def runtime_work(
        kind: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> dict[str, Any]:
        try:
            return runtime_observability.list_work(
                kind=kind,
                status=status,
                cursor=cursor,
                limit=limit,
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/runtime/projections/{event_id}/retry")
    def retry_runtime_projection(event_id: str) -> dict[str, Any]:
        try:
            return runtime_observability.retry_projection(event_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Projection event not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/research-commands", status_code=202)
    def create_research_command(
        payload: ResearchCommandSubmission,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=8, max_length=200),
        ],
    ) -> dict[str, Any]:
        try:
            return research_commands.submit(
                payload,
                idempotency_key=idempotency_key,
            ).model_dump()
        except ResearchCommandConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/research-commands/{command_id}")
    def get_research_command(command_id: str) -> dict[str, Any]:
        try:
            return research_commands.get(command_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Research command not found.") from exc

    @app.post("/api/v1/research-commands/{command_id}/retry", status_code=202)
    def retry_research_command(command_id: str) -> dict[str, Any]:
        try:
            return research_commands.retry(command_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Research command not found.") from exc
        except (ValueError, LiveLLMRequiredError, AgentRunConflictError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/knowledge/ingestions", status_code=202)
    def submit_knowledge_ingestion(payload: KnowledgeIngestionRequest) -> dict[str, Any]:
        try:
            return service.submit(**payload.model_dump()).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/knowledge/ingestions/execute", status_code=202)
    def submit_and_execute_knowledge_ingestion(
        payload: KnowledgeIngestionRequest,
    ) -> dict[str, Any]:
        try:
            ingestion = service.submit(**payload.model_dump(), auto_execute=True)
            return ingestion.model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/knowledge/ingestions")
    def list_knowledge_ingestions() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.list_ingestions()]

    @app.patch("/api/knowledge/ingestions/{ingestion_id}/collection")
    def move_knowledge_ingestion_collection(
        ingestion_id: str, payload: CollectionRequest
    ) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return repository.move_ingestion_collection(
                ingestion_id, payload.collection
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/knowledge/ingestions/{ingestion_id}")
    def get_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        return _require_ingestion(repository, ingestion_id).model_dump()

    @app.post("/api/knowledge/ingestions/{ingestion_id}/retry", status_code=202)
    def retry_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            ingestion = service.retry(ingestion_id, auto_execute=True)
            return ingestion.model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/knowledge/ingestions/{ingestion_id}/execute", status_code=202)
    def execute_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            service.ensure_execution_available()
            ingestion = repository.mark_ingestion_for_dispatch(ingestion_id)
            return ingestion.model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/knowledge/ingestions/{ingestion_id}/candidates")
    def list_knowledge_candidates(
        ingestion_id: str,
        status: CandidateStatus | None = None,
    ) -> list[dict[str, Any]]:
        _require_ingestion(repository, ingestion_id)
        return repository.list_candidates(ingestion_id, status=status)

    @app.get("/api/knowledge/ingestions/{ingestion_id}/candidate-page")
    def list_knowledge_candidate_page(
        ingestion_id: str,
        status: CandidateStatus | None = "draft",
        kind: Literal["entity", "relation"] | None = None,
        paper_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
        min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        return repository.list_candidates_page(
            ingestion_id,
            status=status,
            kind=kind,
            paper_id=paper_id,
            min_confidence=min_confidence,
            offset=offset,
            limit=limit,
        )

    @app.patch("/api/knowledge/candidates/{candidate_id}")
    def patch_knowledge_candidate(
        candidate_id: str, payload: KnowledgeCandidatePatch
    ) -> dict[str, Any]:
        try:
            return repository.update_candidate(candidate_id, payload.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge candidate not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/knowledge/candidates/{candidate_id}/decision")
    def decide_knowledge_candidate(candidate_id: str, payload: CandidateDecision) -> dict[str, Any]:
        try:
            return service.decide(candidate_id, payload).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge candidate not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/knowledge/ingestions/{ingestion_id}/candidates/bulk-decision")
    def decide_knowledge_candidates_bulk(
        ingestion_id: str, payload: BulkCandidateDecision
    ) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.decide_bulk(ingestion_id, payload).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge candidate not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/knowledge/ingestions/{ingestion_id}/auto-approve-high-confidence")
    def auto_approve_high_confidence_candidates(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.auto_approve_confident(ingestion_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/knowledge/ingestions/{ingestion_id}/approve-ready")
    def approve_ready_knowledge_candidates(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.approve_ready(ingestion_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/knowledge/topics")
    def list_knowledge_topics() -> list[dict[str, Any]]:
        return repository.list_topics()

    @app.get("/api/knowledge/collections")
    def list_knowledge_collections() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.list_collections()]

    @app.post("/api/knowledge/collections", status_code=201)
    def create_knowledge_collection(payload: CollectionRequest) -> dict[str, Any]:
        try:
            return repository.create_collection(payload.collection).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/knowledge/collections/{collection_slug}")
    def get_knowledge_collection(collection_slug: str) -> dict[str, Any]:
        try:
            result = service.collection(collection_slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge collection not found.") from exc
        return result

    @app.get("/api/knowledge/topics/{topic_slug}")
    def get_knowledge_topic(topic_slug: str) -> dict[str, Any]:
        result = service.topic(topic_slug)
        if result["topic"] is None:
            raise HTTPException(status_code=404, detail="Knowledge topic not found.")
        return result

    @app.get("/api/knowledge/graph")
    def get_knowledge_graph(
        topic_slug: str | None = None, collection_slug: str | None = None
    ) -> dict[str, Any]:
        if topic_slug and collection_slug and topic_slug != collection_slug:
            raise HTTPException(status_code=422, detail="topic_slug 与 collection_slug 必须一致。")
        return service.graph(collection_slug or topic_slug)

    @app.get("/api/knowledge/entities/{entity_id}")
    def get_knowledge_entity_detail(entity_id: str) -> dict[str, Any]:
        try:
            return service.entity_detail(entity_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge entity not found.") from exc

    @app.get("/api/knowledge/search")
    def search_knowledge(
        q: Annotated[str, Query(min_length=2, max_length=1000)],
        topic_slug: Annotated[list[str] | None, Query()] = None,
        collection_slug: Annotated[list[str] | None, Query()] = None,
        top_k: Annotated[int, Query(ge=1, le=30)] = 8,
    ) -> dict[str, Any]:
        if topic_slug and collection_slug and topic_slug != collection_slug:
            raise HTTPException(status_code=422, detail="topic_slug 与 collection_slug 必须一致。")
        try:
            selected_collections = collection_slug or topic_slug or []
            return reports.query_service.search(
                q,
                topic_slugs=selected_collections,
                top_k=top_k,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/reports", status_code=202)
    def submit_report(payload: ReportCreateRequest) -> dict[str, Any]:
        return submit_report_request(payload, auto_execute=False)

    def submit_report_request(
        payload: ReportCreateRequest,
        *,
        auto_execute: bool,
    ) -> dict[str, Any]:
        try:
            values = payload.model_dump()
            values["topic_slugs"] = values.pop("collection_slugs") or values["topic_slugs"]
            return reports.submit(**values, auto_execute=auto_execute).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def dispatch_report(
        report_id: str,
        *,
        retry_failed: bool,
    ) -> dict[str, Any]:
        report = _require_report(repository, report_id)
        if retry_failed:
            try:
                report = repository.reset_report_for_retry(report_id, auto_execute=True)
            except KeyError as exc:
                raise HTTPException(status_code=409, detail="Report job is missing.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return report.model_dump()
        elif report.status not in {"queued", "running"}:
            action = "retry" if report.status == "failed" else "create a new report"
            raise HTTPException(
                status_code=409,
                detail=f"Report is {report.status}; {action} instead.",
            )
        try:
            report = repository.mark_report_for_dispatch(report_id)
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="Report job is missing.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return report.model_dump()

    @app.post("/api/reports/execute", status_code=202)
    def submit_and_execute_report(payload: ReportCreateRequest) -> dict[str, Any]:
        report = submit_report_request(payload, auto_execute=True)
        return report

    @app.post("/api/reports/{report_id}/execute", status_code=202)
    def execute_report(report_id: str) -> dict[str, Any]:
        return dispatch_report(report_id, retry_failed=False)

    @app.post("/api/reports/{report_id}/retry", status_code=202)
    def retry_report(report_id: str) -> dict[str, Any]:
        return dispatch_report(report_id, retry_failed=True)

    @app.get("/api/reports")
    def list_reports() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.list_reports()]

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str) -> dict[str, Any]:
        return _require_report(repository, report_id).model_dump()

    @app.get("/api/reports/{report_id}/evidence")
    def get_report_evidence(report_id: str) -> dict[str, Any]:
        report = _require_report(repository, report_id)
        return {
            "report_id": report.id,
            "evidence": [item.model_dump() for item in report.evidence],
            "evaluation": report.evaluation.model_dump() if report.evaluation else None,
        }

    @app.get("/api/reports/{report_id}/download")
    def download_report(report_id: str) -> Response:
        report = _require_report(repository, report_id)
        if report.status != "completed":
            raise HTTPException(status_code=409, detail=f"Report is {report.status}.")
        return Response(
            content=report.content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report.id}.md"'},
        )

    # Memory Core --------------------------------------------------------------

    # Phase 4 browser-only projections are versioned independently from the
    # established Core, Memory, and Agent lifecycle APIs above and below.
    @app.get("/api/v1/workspace/dashboard")
    def get_workspace_dashboard(
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        return _workspace_call(lambda: workspace.dashboard(limit=limit)).model_dump()

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreateRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.create_project(payload)).model_dump()

    @app.get("/api/projects")
    def list_projects(include_archived: bool = False) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(
                lambda: memory.repository.list_projects(include_archived=include_archived)
            )
        ]

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.repository.get_project(project_id)).model_dump()

    @app.patch("/api/projects/{project_id}")
    def update_project(project_id: str, payload: ProjectUpdateRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.update_project(project_id, payload)).model_dump()

    @app.patch("/api/projects/{project_id}/status")
    def transition_project(project_id: str, payload: ProjectStatusRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.transition_project(project_id, payload)).model_dump()

    @app.post("/api/projects/{project_id}/archive")
    def archive_project(project_id: str, payload: RevisionRequest) -> dict[str, Any]:
        status_payload = ProjectStatusRequest(
            expected_revision=payload.expected_revision, status="archived"
        )
        return _memory_call(
            lambda: memory.transition_project(project_id, status_payload)
        ).model_dump()

    @app.get("/api/projects/{project_id}/memory")
    def get_project_memory_snapshot(project_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.snapshot(project_id)).model_dump()

    @app.get("/api/projects/{project_id}/knowledge-scopes")
    def list_project_knowledge_scopes(project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(
                lambda: memory.repository.list_project_knowledge_scopes(project_id)
            )
        ]

    @app.put("/api/projects/{project_id}/knowledge-scopes")
    def replace_project_knowledge_scopes(
        project_id: str, payload: ProjectKnowledgeScopeReplaceRequest
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(lambda: memory.replace_knowledge_scopes(project_id, payload))
        ]

    # Domain Plugin routing ---------------------------------------------------

    @app.get("/api/v1/domain-plugins")
    def list_domain_plugins() -> list[dict[str, Any]]:
        return [item.model_dump() for item in domain_plugins.list_available()]

    @app.get("/api/v1/projects/{project_id}/domain-plugins")
    def list_project_domain_plugins(project_id: str) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _domain_plugin_call(
                lambda: domain_plugins.list_project_bindings(project_id)
            )
        ]

    @app.put("/api/v1/projects/{project_id}/domain-plugins/{plugin_key}")
    def set_project_domain_plugin(
        project_id: str,
        plugin_key: str,
        payload: ProjectDomainPluginUpdateRequest,
    ) -> dict[str, Any]:
        return _domain_plugin_call(
            lambda: domain_plugins.set_project_binding(project_id, plugin_key, payload)
        ).model_dump()

    @app.patch("/api/v1/workspace-tasks/{task_id}/domain-plugin")
    def bind_workspace_task_domain_plugin(
        task_id: str, payload: WorkspaceTaskPluginBindRequest
    ) -> dict[str, Any]:
        return _domain_plugin_call(
            lambda: domain_plugins.bind_workspace_task(task_id, payload)
        ).model_dump()

    # Game Modeling Knowledge authoring -------------------------------------
    #
    # These endpoints intentionally compose the existing Candidate → Review →
    # PublishedEntity lifecycle. They do not expose direct writes to Core
    # Entity/Claim tables or a Game-specific persistence path.
    @app.post("/api/v1/domain-plugins/game_modeling/patch-candidates", status_code=201)
    def create_game_patch_candidate(
        payload: GamePatchCandidateCreateRequest,
    ) -> dict[str, Any]:
        return _game_knowledge_call(
            lambda: game_knowledge.create_patch_candidate(payload)
        ).model_dump()

    @app.post("/api/v1/domain-plugins/game_modeling/formula-candidates", status_code=201)
    def create_game_formula_candidate(
        payload: GameFormulaCandidateCreateRequest,
    ) -> dict[str, Any]:
        return _game_knowledge_call(
            lambda: game_knowledge.create_formula_candidate(payload)
        ).model_dump()

    @app.post("/api/v1/domain-plugins/game_modeling/candidates/{candidate_id}/approve")
    def approve_game_knowledge_candidate(candidate_id: str) -> dict[str, Any]:
        return _game_knowledge_call(
            lambda: game_knowledge.approve_candidate(candidate_id)
        ).model_dump()

    @app.post("/api/v1/domain-plugins/game_modeling/candidates/{candidate_id}/reject")
    def reject_game_knowledge_candidate(
        candidate_id: str, payload: GameKnowledgeReviewRequest
    ) -> dict[str, Any]:
        return _game_knowledge_call(
            lambda: game_knowledge.reject_candidate(
                candidate_id, review_note=payload.review_note
            )
        ).model_dump()

    @app.post("/api/projects/{project_id}/workspace-tasks", status_code=201)
    def create_workspace_task(
        project_id: str, payload: WorkspaceTaskCreateRequest
    ) -> dict[str, Any]:
        return _memory_call(lambda: memory.create_workspace_task(project_id, payload)).model_dump()

    @app.get("/api/projects/{project_id}/workspace-tasks")
    def list_workspace_tasks(
        project_id: str, include_closed: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(
                lambda: memory.repository.list_workspace_tasks(
                    project_id, include_closed=include_closed
                )
            )
        ]

    @app.get("/api/workspace-tasks/{task_id}")
    def get_workspace_task(task_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.repository.get_workspace_task(task_id)).model_dump()

    @app.patch("/api/workspace-tasks/{task_id}")
    def update_workspace_task(task_id: str, payload: WorkspaceTaskUpdateRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.update_workspace_task(task_id, payload)).model_dump()

    @app.patch("/api/workspace-tasks/{task_id}/status")
    def transition_workspace_task(
        task_id: str, payload: WorkspaceTaskStatusRequest
    ) -> dict[str, Any]:
        return _memory_call(lambda: memory.transition_workspace_task(task_id, payload)).model_dump()

    @app.post("/api/projects/{project_id}/workspace-tasks/{task_id}/agent-runs", status_code=202)
    def create_agent_run(
        project_id: str, task_id: str, payload: AgentRunCreateRequest
    ) -> dict[str, Any]:
        _memory_call(lambda: memory.repository.get_project(project_id))
        return _agent_call(lambda: agents.create_run(project_id, task_id, payload)).model_dump()

    @app.get("/api/v1/projects/{project_id}/workspace-tasks/{task_id}/agent-runs")
    def list_task_agent_runs(
        project_id: str,
        task_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.list_task_runs(
                project_id, task_id, limit=limit, cursor=cursor
            )
        )

    @app.get("/api/v1/projects/{project_id}/agent-runs")
    def list_project_agent_runs(
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.list_project_runs(project_id, limit=limit, cursor=cursor)
        )

    @app.post(
        "/api/v1/projects/{project_id}/workspace-tasks/{task_id}/context-snapshots/preview"
    )
    def preview_workspace_task_context(
        project_id: str, task_id: str, payload: ContextProjectionRequest
    ) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.preview_context(project_id, task_id, payload)
        ).model_dump()

    @app.post(
        "/api/v1/projects/{project_id}/workspace-tasks/{task_id}/context-snapshots",
        status_code=201,
    )
    def create_workspace_task_context_snapshot(
        project_id: str, task_id: str, payload: ContextProjectionRequest
    ) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.create_context_snapshot(project_id, task_id, payload)
        ).model_dump()

    @app.get("/api/v1/context-snapshots/{snapshot_id}")
    def get_context_snapshot_summary(snapshot_id: str) -> dict[str, Any]:
        return _workspace_call(lambda: workspace.get_context_snapshot(snapshot_id)).model_dump()

    @app.get("/api/v1/context-snapshots/{snapshot_id}/items")
    def list_context_snapshot_items(
        snapshot_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.list_context_snapshot_items(
                snapshot_id, offset=offset, limit=limit
            )
        ).model_dump()

    @app.get("/api/v1/context-snapshots/{snapshot_id}/evidence/{evidence_id}")
    def get_context_snapshot_evidence(snapshot_id: str, evidence_id: str) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.get_snapshot_evidence(snapshot_id, evidence_id)
        ).model_dump()

    @app.get("/api/agent-runs/{run_id}")
    def get_agent_run(run_id: str) -> dict[str, Any]:
        return _agent_call(lambda: agents.get_run(run_id)).model_dump()

    @app.get("/api/agent-runs/{run_id}/events")
    def list_agent_run_events(run_id: str) -> list[dict[str, Any]]:
        events = _agent_call(lambda: agents.repository.list_events(run_id))
        return [item.model_dump() for item in events]

    @app.get("/api/agent-runs/{run_id}/tool-calls")
    def list_agent_tool_calls(run_id: str) -> list[dict[str, Any]]:
        tool_calls = _agent_call(lambda: agents.repository.list_tool_calls(run_id))
        return [item.model_dump() for item in tool_calls]

    @app.get("/api/v1/agent-runs/{run_id}/trace")
    def get_agent_run_trace(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return _workspace_call(
            lambda: workspace.get_run_trace(
                run_id, after_sequence=after_sequence, limit=limit
            )
        ).model_dump()

    @app.get("/api/agent-runs/{run_id}/output")
    def get_agent_run_output(run_id: str) -> dict[str, Any]:
        return _agent_call(lambda: agents.repository.get_output(run_id)).model_dump()

    @app.post("/api/agent-runs/{run_id}/cancel")
    def cancel_agent_run(run_id: str) -> dict[str, Any]:
        return _agent_call(lambda: agents.cancel_run(run_id)).model_dump()

    @app.post("/api/agent-runs/{run_id}/resume", status_code=202)
    def resume_agent_run(run_id: str) -> dict[str, Any]:
        return _agent_call(lambda: agents.resume_run(run_id)).model_dump()

    @app.post("/api/projects/{project_id}/decisions", status_code=201)
    def create_memory_decision(project_id: str, payload: DecisionCreateRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.create_decision(project_id, payload)).model_dump()

    @app.get("/api/projects/{project_id}/decisions")
    def list_memory_decisions(
        project_id: str, include_inactive: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(
                lambda: memory.repository.list_decisions(
                    project_id, include_inactive=include_inactive
                )
            )
        ]

    @app.get("/api/decisions/{decision_id}")
    def get_memory_decision(decision_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.repository.get_decision(decision_id)).model_dump()

    @app.patch("/api/decisions/{decision_id}/status")
    def transition_memory_decision(
        decision_id: str, payload: DecisionStatusRequest
    ) -> dict[str, Any]:
        return _memory_call(lambda: memory.transition_decision(decision_id, payload)).model_dump()

    @app.post("/api/projects/{project_id}/artifacts", status_code=201)
    def create_artifact(project_id: str, payload: ArtifactCreateRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.create_artifact(project_id, payload)).model_dump()

    @app.get("/api/projects/{project_id}/artifacts")
    def list_artifacts(project_id: str, include_inactive: bool = False) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(
                lambda: memory.repository.list_artifacts(
                    project_id, include_inactive=include_inactive
                )
            )
        ]

    @app.get("/api/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.repository.get_artifact(artifact_id)).model_dump()

    @app.get("/api/v1/artifacts/{artifact_id}/content")
    def get_artifact_content(artifact_id: str) -> dict[str, Any]:
        return _workspace_call(lambda: workspace.get_artifact_content(artifact_id)).model_dump()

    @app.post("/api/artifacts/{artifact_id}/versions", status_code=201)
    def create_next_artifact_version(
        artifact_id: str, payload: ArtifactNextVersionRequest
    ) -> dict[str, Any]:
        return _memory_call(
            lambda: memory.create_next_artifact_version(artifact_id, payload)
        ).model_dump()

    @app.patch("/api/artifacts/{artifact_id}/status")
    def transition_artifact(artifact_id: str, payload: ArtifactStatusRequest) -> dict[str, Any]:
        return _memory_call(lambda: memory.transition_artifact(artifact_id, payload)).model_dump()

    @app.post("/api/projects/{project_id}/memory-proposals", status_code=201)
    def create_memory_proposal(
        project_id: str, payload: MemoryProposalCreateRequest
    ) -> dict[str, Any]:
        return _memory_call(lambda: memory.create_proposal(project_id, payload)).model_dump()

    @app.get("/api/projects/{project_id}/memory-proposals")
    def list_memory_proposals(
        project_id: str, include_closed: bool = False
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump()
            for item in _memory_call(
                lambda: memory.repository.list_proposals(project_id, include_closed=include_closed)
            )
        ]

    @app.get("/api/memory-proposals/{proposal_id}")
    def get_memory_proposal(proposal_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.repository.get_proposal(proposal_id)).model_dump()

    @app.post("/api/memory-proposals/{proposal_id}/review")
    def review_memory_proposal(
        proposal_id: str, payload: MemoryProposalReviewRequest
    ) -> dict[str, Any]:
        return _memory_call(lambda: memory.review_proposal(proposal_id, payload)).model_dump()

    @app.post("/api/memory-proposals/{proposal_id}/commit")
    def commit_memory_proposal(proposal_id: str) -> dict[str, Any]:
        return _memory_call(lambda: memory.commit_proposal(proposal_id)).model_dump()

    return app


def _require_ingestion(repository: KnowledgeRepository, ingestion_id: str):
    try:
        return repository.get_ingestion(ingestion_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Knowledge ingestion not found.") from exc


def _require_report(repository: KnowledgeRepository, report_id: str):
    try:
        return repository.get_report(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Report not found.") from exc


def _memory_call(callback):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _agent_call(callback):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        AgentRunConflictError,
        AgentRunNotRecoverableError,
        AgentRunTerminalError,
        ResearchLLMRequiredError,
        DomainPluginConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _domain_plugin_call(callback):
    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DomainPluginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _game_knowledge_call(callback):
    """Map Game authoring to the existing API error vocabulary."""

    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DomainPluginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _workspace_call(callback):
    """Map bounded UI projections to existing API error semantics."""

    try:
        return callback()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ContextConflictError, WorkspaceProjectionConflictError, SnapshotIntegrityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ContextBudgetTooSmallError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _service_status(endpoint: str) -> dict[str, str | bool]:
    parsed = urlparse(endpoint if "://" in endpoint else f"bolt://{endpoint}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (7687 if parsed.scheme == "bolt" else 6333)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return {"configured": True, "available": True, "endpoint": endpoint}
    except OSError:
        return {"configured": True, "available": False, "endpoint": endpoint}
