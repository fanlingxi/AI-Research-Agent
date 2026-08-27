from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI

from app.agent.service import AgentRunService
from app.config.settings import get_settings
from app.domain_plugins.game_modeling.knowledge import GameKnowledgeAuthoringService
from app.domain_plugins.service import DomainPluginService
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeIngestionService
from app.memory.service import MemoryService
from app.research_commands.service import ResearchCommandService
from app.runtime.service import RuntimeObservabilityService
from app.workspace.service import WorkspaceProjectionService

from .routers.agent import build_agent_router
from .routers.knowledge import build_knowledge_router
from .routers.projects import build_projects_router
from .routers.reports import build_reports_router
from .routers.runtime import build_runtime_router


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
    """Compose the modular monolith without executing long-running work."""

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
                "core_shadow": repository.knowledge_core_shadow_read(),
                "vault": settings.knowledge_vault_path,
                "llm_provider": getattr(service.llm, "provider_name", "mock"),
                "live_llm_configured": not service._is_mock_llm(),
                "jobs": repository.job_summary(),
                "projections": repository.projection_summary(),
            },
        }

    @app.get("/health", tags=["health"])
    def health() -> dict[str, Any]:
        return health_payload()

    @app.get("/api/knowledge/health", tags=["health"])
    def knowledge_health() -> dict[str, Any]:
        return health_payload()

    app.include_router(build_runtime_router(runtime_observability, research_commands))
    app.include_router(build_knowledge_router(repository, service, reports))
    app.include_router(build_reports_router(repository, reports))
    app.include_router(build_projects_router(memory, domain_plugins, game_knowledge, workspace))
    app.include_router(build_agent_router(memory, agents, workspace))
    return app


def _service_status(endpoint: str) -> dict[str, str | bool]:
    parsed = urlparse(endpoint if "://" in endpoint else f"bolt://{endpoint}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (7687 if parsed.scheme == "bolt" else 6333)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return {"configured": True, "available": True, "endpoint": endpoint}
    except OSError:
        return {"configured": True, "available": False, "endpoint": endpoint}
