from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.config.settings import get_settings
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import CandidateDecision, CandidateStatus
from app.knowledge.service import KnowledgeIngestionService


class KnowledgeIngestionRequest(BaseModel):
    topic: str = Field(min_length=3, max_length=500)
    sources: list[str] = Field(min_length=1, max_length=30)
    pdf_max_pages: int = Field(default=20, ge=1, le=100)


class KnowledgeCandidatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    summary: str | None = Field(default=None, min_length=12, max_length=900)
    aliases: list[str] | None = Field(default=None, max_length=12)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ReportCreateRequest(BaseModel):
    query: str = Field(min_length=3, max_length=1000)
    topic_slugs: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=8, ge=1, le=30)
    report_depth: Literal["brief", "standard", "deep"] = "standard"


def create_app(
    knowledge_repository: KnowledgeRepository | None = None,
    knowledge_service: KnowledgeIngestionService | None = None,
    report_service: KnowledgeReportService | None = None,
) -> FastAPI:
    settings = get_settings()
    repository = knowledge_repository or KnowledgeRepository(settings.knowledge_db_path)
    service = knowledge_service or KnowledgeIngestionService(repository, settings=settings)
    reports = report_service or KnowledgeReportService(repository, settings=settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.knowledge_repository = repository
        app.state.knowledge_service = service
        app.state.report_service = reports
        repository.recover_running_work()
        yield

    app = FastAPI(
        title="AI Research Knowledge Core API",
        version="0.2.0",
        description="PDF-first, reviewable research knowledge and grounded reports.",
        lifespan=lifespan,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "services": {
                "qdrant": _service_status(settings.qdrant_url),
                "neo4j": _service_status(settings.neo4j_uri),
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

    @app.post("/api/knowledge/ingestions", status_code=202)
    def submit_knowledge_ingestion(payload: KnowledgeIngestionRequest) -> dict[str, Any]:
        try:
            return service.submit(**payload.model_dump()).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/knowledge/ingestions")
    def list_knowledge_ingestions() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.list_ingestions()]

    @app.get("/api/knowledge/ingestions/{ingestion_id}")
    def get_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        return _require_ingestion(repository, ingestion_id).model_dump()

    @app.post("/api/knowledge/ingestions/{ingestion_id}/retry", status_code=202)
    def retry_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.retry(ingestion_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/knowledge/ingestions/{ingestion_id}/candidates")
    def list_knowledge_candidates(
        ingestion_id: str,
        status: CandidateStatus | None = None,
    ) -> list[dict[str, Any]]:
        _require_ingestion(repository, ingestion_id)
        return repository.list_candidates(ingestion_id, status=status)

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

    @app.get("/api/knowledge/topics/{topic_slug}")
    def get_knowledge_topic(topic_slug: str) -> dict[str, Any]:
        result = service.topic(topic_slug)
        if result["topic"] is None:
            raise HTTPException(status_code=404, detail="Knowledge topic not found.")
        return result

    @app.get("/api/knowledge/graph")
    def get_knowledge_graph(topic_slug: str | None = None) -> dict[str, Any]:
        return service.graph(topic_slug)

    @app.get("/api/knowledge/search")
    def search_knowledge(
        q: Annotated[str, Query(min_length=2, max_length=1000)],
        topic_slug: Annotated[list[str] | None, Query()] = None,
        top_k: Annotated[int, Query(ge=1, le=30)] = 8,
    ) -> dict[str, Any]:
        try:
            return reports.query_service.search(
                q,
                topic_slugs=topic_slug or [],
                top_k=top_k,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/reports", status_code=202)
    def submit_report(payload: ReportCreateRequest) -> dict[str, Any]:
        try:
            return reports.submit(**payload.model_dump()).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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


def _service_status(endpoint: str) -> dict[str, str | bool]:
    parsed = urlparse(endpoint if "://" in endpoint else f"bolt://{endpoint}")
    host = parsed.hostname or "localhost"
    port = parsed.port or (7687 if parsed.scheme == "bolt" else 6333)
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return {"configured": True, "available": True, "endpoint": endpoint}
    except OSError:
        return {"configured": True, "available": False, "endpoint": endpoint}


app = create_app()
