from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import CollectionRequest, KnowledgeCandidatePatch, KnowledgeIngestionRequest
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import BulkCandidateDecision, CandidateDecision, CandidateStatus
from app.knowledge.service import KnowledgeIngestionService


def _require_ingestion(repository: KnowledgeRepository, ingestion_id: str):
    try:
        return repository.get_ingestion(ingestion_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Knowledge ingestion not found.") from exc


def build_knowledge_router(
    repository: KnowledgeRepository,
    service: KnowledgeIngestionService,
    reports: KnowledgeReportService,
) -> APIRouter:
    router = APIRouter(tags=["knowledge"])

    @router.post("/api/knowledge/ingestions", status_code=202)
    def submit_knowledge_ingestion(payload: KnowledgeIngestionRequest) -> dict[str, Any]:
        try:
            return service.submit(**payload.model_dump()).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/api/knowledge/ingestions/execute", status_code=202)
    def submit_and_execute_knowledge_ingestion(
        payload: KnowledgeIngestionRequest,
    ) -> dict[str, Any]:
        try:
            return service.submit(**payload.model_dump(), auto_execute=True).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/knowledge/ingestions")
    def list_knowledge_ingestions() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.list_ingestions()]

    @router.patch("/api/knowledge/ingestions/{ingestion_id}/collection")
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

    @router.get("/api/knowledge/ingestions/{ingestion_id}")
    def get_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        return _require_ingestion(repository, ingestion_id).model_dump()

    @router.post("/api/knowledge/ingestions/{ingestion_id}/retry", status_code=202)
    def retry_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.retry(ingestion_id, auto_execute=True).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/knowledge/ingestions/{ingestion_id}/execute", status_code=202)
    def execute_knowledge_ingestion(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            service.ensure_execution_available()
            return repository.mark_ingestion_for_dispatch(ingestion_id).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/knowledge/ingestions/{ingestion_id}/candidates")
    def list_knowledge_candidates(
        ingestion_id: str,
        status: CandidateStatus | None = None,
    ) -> list[dict[str, Any]]:
        _require_ingestion(repository, ingestion_id)
        return repository.list_candidates(ingestion_id, status=status)

    @router.get("/api/knowledge/ingestions/{ingestion_id}/candidate-page")
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

    @router.patch("/api/knowledge/candidates/{candidate_id}")
    def patch_knowledge_candidate(
        candidate_id: str, payload: KnowledgeCandidatePatch
    ) -> dict[str, Any]:
        try:
            return repository.update_candidate(candidate_id, payload.model_dump(exclude_none=True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge candidate not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/knowledge/candidates/{candidate_id}/decision")
    def decide_knowledge_candidate(
        candidate_id: str, payload: CandidateDecision
    ) -> dict[str, Any]:
        try:
            return service.decide(candidate_id, payload).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge candidate not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/knowledge/ingestions/{ingestion_id}/candidates/bulk-decision")
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

    @router.post("/api/knowledge/ingestions/{ingestion_id}/auto-approve-high-confidence")
    def auto_approve_high_confidence_candidates(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.auto_approve_confident(ingestion_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/knowledge/ingestions/{ingestion_id}/approve-ready")
    def approve_ready_knowledge_candidates(ingestion_id: str) -> dict[str, Any]:
        _require_ingestion(repository, ingestion_id)
        try:
            return service.approve_ready(ingestion_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/knowledge/topics")
    def list_knowledge_topics() -> list[dict[str, Any]]:
        return repository.list_topics()

    @router.get("/api/knowledge/collections")
    def list_knowledge_collections() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.list_collections()]

    @router.post("/api/knowledge/collections", status_code=201)
    def create_knowledge_collection(payload: CollectionRequest) -> dict[str, Any]:
        try:
            return repository.create_collection(payload.collection).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/api/knowledge/collections/{collection_slug}")
    def get_knowledge_collection(collection_slug: str) -> dict[str, Any]:
        try:
            return service.collection(collection_slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge collection not found.") from exc

    @router.get("/api/knowledge/topics/{topic_slug}")
    def get_knowledge_topic(topic_slug: str) -> dict[str, Any]:
        result = service.topic(topic_slug)
        if result["topic"] is None:
            raise HTTPException(status_code=404, detail="Knowledge topic not found.")
        return result

    @router.get("/api/knowledge/graph")
    def get_knowledge_graph(
        topic_slug: str | None = None, collection_slug: str | None = None
    ) -> dict[str, Any]:
        if topic_slug and collection_slug and topic_slug != collection_slug:
            raise HTTPException(status_code=422, detail="topic_slug 与 collection_slug 必须一致。")
        return service.graph(collection_slug or topic_slug)

    @router.get("/api/knowledge/entities/{entity_id}")
    def get_knowledge_entity_detail(entity_id: str) -> dict[str, Any]:
        try:
            return service.entity_detail(entity_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Knowledge entity not found.") from exc

    @router.get("/api/knowledge/search")
    def search_knowledge(
        q: Annotated[str, Query(min_length=2, max_length=1000)],
        topic_slug: Annotated[list[str] | None, Query()] = None,
        collection_slug: Annotated[list[str] | None, Query()] = None,
        top_k: Annotated[int, Query(ge=1, le=30)] = 8,
    ) -> dict[str, Any]:
        if topic_slug and collection_slug and topic_slug != collection_slug:
            raise HTTPException(status_code=422, detail="topic_slug 与 collection_slug 必须一致。")
        try:
            return reports.query_service.search(
                q,
                topic_slugs=collection_slug or topic_slug or [],
                top_k=top_k,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router
