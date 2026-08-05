from __future__ import annotations

import time

from app.config.settings import get_settings
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeIngestionService


class KnowledgeWorker:
    """Single-process durable worker for local Knowledge Core deployments."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        ingestion_service: KnowledgeIngestionService,
        report_service: KnowledgeReportService,
        lease_seconds: int = 180,
    ) -> None:
        self.repository = repository
        self.ingestion_service = ingestion_service
        self.report_service = report_service
        self.lease_seconds = lease_seconds

    def run_once(self) -> bool:
        job = self.repository.claim_job(self.lease_seconds)
        if job is not None:
            try:
                if job.kind == "ingestion":
                    result = self.ingestion_service.run(job.resource_id)
                    if result.status == "failed":
                        raise RuntimeError(result.error or "知识入库失败")
                else:
                    result = self.report_service.run(job.resource_id)
                    if result.status == "failed":
                        raise RuntimeError(result.error or "报告生成失败")
                self.repository.complete_job(job.id)
            except Exception as exc:
                self.repository.fail_job(job.id, str(exc))
            return True

        projection = self.repository.claim_projection(self.lease_seconds)
        if projection is not None:
            try:
                self.ingestion_service.process_projection(projection)
            except Exception:
                pass
            return True
        return False

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        self.repository.recover_running_work()
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)


def build_worker() -> KnowledgeWorker:
    settings = get_settings()
    repository = KnowledgeRepository(settings.knowledge_db_path)
    return KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(repository, settings=settings),
        report_service=KnowledgeReportService(repository, settings=settings),
        lease_seconds=settings.knowledge_worker_lease_seconds,
    )


def main() -> None:
    settings = get_settings()
    build_worker().run_forever(settings.knowledge_worker_poll_seconds)


if __name__ == "__main__":
    main()
