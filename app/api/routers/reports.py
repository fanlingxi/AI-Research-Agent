from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.api.schemas import ReportCreateRequest
from app.knowledge.extractor import LiveLLMRequiredError
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository


def _require_report(repository: KnowledgeRepository, report_id: str):
    try:
        return repository.reports.get_report(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Report not found.") from exc


def build_reports_router(
    repository: KnowledgeRepository,
    reports: KnowledgeReportService,
) -> APIRouter:
    router = APIRouter(tags=["reports"])

    def submit_request(payload: ReportCreateRequest, *, auto_execute: bool) -> dict[str, Any]:
        try:
            values = payload.model_dump()
            values["topic_slugs"] = values.pop("collection_slugs") or values["topic_slugs"]
            return reports.submit(**values, auto_execute=auto_execute).model_dump()
        except LiveLLMRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def dispatch(report_id: str, *, retry_failed: bool) -> dict[str, Any]:
        report = _require_report(repository, report_id)
        if retry_failed:
            try:
                return repository.reports.reset_report_for_retry(
                    report_id, auto_execute=True
                ).model_dump()
            except KeyError as exc:
                raise HTTPException(status_code=409, detail="Report job is missing.") from exc
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if report.status not in {"queued", "running"}:
            action = "retry" if report.status == "failed" else "create a new report"
            raise HTTPException(
                status_code=409,
                detail=f"Report is {report.status}; {action} instead.",
            )
        try:
            return repository.reports.mark_report_for_dispatch(report_id).model_dump()
        except KeyError as exc:
            raise HTTPException(status_code=409, detail="Report job is missing.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/reports", status_code=202)
    def submit_report(payload: ReportCreateRequest) -> dict[str, Any]:
        return submit_request(payload, auto_execute=False)

    @router.post("/api/reports/execute", status_code=202)
    def submit_and_execute_report(payload: ReportCreateRequest) -> dict[str, Any]:
        return submit_request(payload, auto_execute=True)

    @router.post("/api/reports/{report_id}/execute", status_code=202)
    def execute_report(report_id: str) -> dict[str, Any]:
        return dispatch(report_id, retry_failed=False)

    @router.post("/api/reports/{report_id}/retry", status_code=202)
    def retry_report(report_id: str) -> dict[str, Any]:
        return dispatch(report_id, retry_failed=True)

    @router.get("/api/reports")
    def list_reports() -> list[dict[str, Any]]:
        return [item.model_dump() for item in repository.reports.list_reports()]

    @router.get("/api/reports/{report_id}")
    def get_report(report_id: str) -> dict[str, Any]:
        return _require_report(repository, report_id).model_dump()

    @router.get("/api/reports/{report_id}/evidence")
    def get_report_evidence(report_id: str) -> dict[str, Any]:
        report = _require_report(repository, report_id)
        return {
            "report_id": report.id,
            "evidence": [item.model_dump() for item in report.evidence],
            "evaluation": report.evaluation.model_dump() if report.evaluation else None,
        }

    @router.get("/api/reports/{report_id}/download")
    def download_report(report_id: str) -> Response:
        report = _require_report(repository, report_id)
        if report.status != "completed":
            raise HTTPException(status_code=409, detail=f"Report is {report.status}.")
        return Response(
            content=report.content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{report.id}.md"'},
        )

    return router
