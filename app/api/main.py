from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config.settings import get_settings
from app.graph.workflow import run_research_workflow
from app.obsidian.exporter import ObsidianVaultExporter
from app.schemas.documents import PaperMetadata, RetrievalHit
from app.schemas.graph import GraphEntity, GraphRelation
from app.schemas.quality import EvaluationResult

from .task_store import ResearchTask, ResearchTaskStore


class ResearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    live_search: bool = False
    paper_limit: int = Field(default=5, ge=1, le=20)
    top_k: int = Field(default=5, ge=1, le=20)
    vector_store_provider: Literal["memory", "qdrant"] = "memory"
    graph_store_provider: Literal["memory", "neo4j"] = "memory"
    memory_enabled: bool = True
    document_sources: list[str] = Field(default_factory=list)
    pdf_max_pages: int = Field(default=12, ge=1, le=100)
    obsidian_export_enabled: bool = False
    obsidian_vault_path: str | None = None


class ObsidianExportRequest(BaseModel):
    vault_path: str | None = None


def create_app(task_store: ResearchTaskStore | None = None) -> FastAPI:
    store = task_store or ResearchTaskStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.task_store = store
        yield

    app = FastAPI(
        title="AI-Research-Agent API",
        version="0.2.0",
        description="Evidence-governed Agentic GraphRAG research service.",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict:
        settings = get_settings()
        return {
            "status": "ok",
            "services": {
                "qdrant": _service_status(settings.qdrant_url),
                "neo4j": _service_status(settings.neo4j_uri),
            },
            "fallbacks": {
                "vector_store": "memory",
                "graph_store": "memory",
                "memory_backend": settings.memory_backend,
            },
        }

    @app.post("/api/research", status_code=202)
    def submit_research(payload: ResearchRequest, background_tasks: BackgroundTasks) -> dict:
        task = store.create()
        background_tasks.add_task(_run_task, store, task.id, payload)
        return task.snapshot()

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        return _require_task(store, task_id).snapshot()

    @app.get("/api/tasks/{task_id}/report")
    def get_report(task_id: str) -> dict:
        task = _require_completed_task(store, task_id)
        return {"task_id": task.id, "report": task.result.get("final_report", "")}

    @app.get("/api/tasks/{task_id}/graph")
    def get_graph(task_id: str) -> dict:
        task = _require_completed_task(store, task_id)
        return {
            "task_id": task.id,
            "nodes": [_dump(item) for item in task.result.get("graph_entities", [])],
            "edges": [_dump(item) for item in task.result.get("graph_relations", [])],
            "paths": [_dump(item) for item in task.result.get("graph_paths", [])],
        }

    @app.post("/api/tasks/{task_id}/obsidian-export")
    def export_to_obsidian(task_id: str, payload: ObsidianExportRequest) -> dict:
        task = _require_completed_task(store, task_id)
        state = task.result
        evaluation = EvaluationResult.model_validate(state["evaluation_result"])
        settings = get_settings()
        exporter = ObsidianVaultExporter(
            vault_path=payload.vault_path or settings.obsidian_vault_path,
            review_status=settings.obsidian_review_status,
        )
        result = exporter.export(
            run_id=state["run_id"],
            query=state["query"],
            report=state.get("final_report", ""),
            evaluation=evaluation,
            papers=[PaperMetadata.model_validate(item) for item in state.get("papers", [])],
            retrieval_hits=[
                RetrievalHit.model_validate(item) for item in state.get("retrieval_results", [])
            ],
            entities=[GraphEntity.model_validate(item) for item in state.get("graph_entities", [])],
            relations=[
                GraphRelation.model_validate(item) for item in state.get("graph_relations", [])
            ],
        )
        state["obsidian_export_result"] = result.model_dump()
        return result.model_dump()

    return app


def _run_task(store: ResearchTaskStore, task_id: str, payload: ResearchRequest) -> None:
    store.start(task_id)
    try:
        result = run_research_workflow(**payload.model_dump())
    except Exception as exc:  # pragma: no cover - FastAPI background boundary
        store.fail(task_id, str(exc))
        return
    store.complete(task_id, result)


def _require_task(store: ResearchTaskStore, task_id: str) -> ResearchTask:
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Research task not found.")
    return task


def _require_completed_task(store: ResearchTaskStore, task_id: str) -> ResearchTask:
    task = _require_task(store, task_id)
    if task.status != "completed" or task.result is None:
        raise HTTPException(status_code=409, detail=f"Task is {task.status}.")
    return task


def _service_status(endpoint: str) -> dict[str, str | bool]:
    parsed = urlparse(endpoint if "://" in endpoint else f"bolt://{endpoint}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (7687 if parsed.scheme == "bolt" else 6333)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return {"configured": True, "available": True, "endpoint": endpoint}
    except OSError:
        return {"configured": True, "available": False, "endpoint": endpoint}


def _dump(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


app = create_app()
