from __future__ import annotations

import os
import socket
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.runtime import AgentRuntime
from app.agent.service import AgentRunService
from app.config.settings import get_settings
from app.execution import ExecutionFence
from app.knowledge.rebuild import KnowledgeProjectionRebuildService
from app.knowledge.rebuild_jobs import ProjectionRebuildJobs, RebuildRequest
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import KnowledgeJob, ProjectionEvent
from app.knowledge.service import KnowledgeIngestionService
from app.research_commands.service import ResearchCommandService


class KnowledgeWorker:
    """Single-process durable worker for local Knowledge Core deployments."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        ingestion_service: KnowledgeIngestionService,
        report_service: KnowledgeReportService,
        agent_runtime: AgentRuntime | None = None,
        research_command_service: ResearchCommandService | None = None,
        lease_seconds: int = 180,
        worker_id: str | None = None,
        version: str = "unified-worker-v2",
    ) -> None:
        self.repository = repository
        self.ingestion_service = ingestion_service
        self.report_service = report_service
        self.agent_runtime = agent_runtime
        self.research_command_service = research_command_service
        self.lease_seconds = lease_seconds
        self.worker_id = worker_id or (
            f"worker-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self.version = version
        self.started_at = datetime.now(tz=UTC).isoformat()

    def run_once(self) -> bool:
        self._heartbeat_executor()
        job = self.repository.jobs.claim_job(
            self.lease_seconds,
            owner_id=self.worker_id,
        )
        if job is not None:
            self._execute_job_with_heartbeat(job)
            return True

        projection = self.repository.claim_projection(
            self.lease_seconds,
            owner_id=self.worker_id,
        )
        if projection is not None:
            self._execute_projection_with_heartbeat(projection)
            return True
        return False

    def _execute_job_with_heartbeat(self, job: KnowledgeJob) -> None:
        stopped = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_job,
            args=(job, stopped),
            daemon=True,
            name=f"worker-job-lease-{job.id}",
        )
        self._heartbeat_executor(job.id)
        heartbeat.start()
        try:
            if job.kind == "ingestion":
                self.ingestion_service.execute_claimed(job)
                return
            if job.kind == "report":
                self.report_service.execute_claimed(
                    job.resource_id,
                    job.id,
                    job.attempts,
                    expected_owner=job.lease_owner,
                )
                return
            if job.kind == "agent_run":
                if self.agent_runtime is None:
                    raise RuntimeError("Agent Runtime is not configured")
                result = self.agent_runtime.execute_queued(
                    job.resource_id,
                    execution_fence=ExecutionFence.from_job(job),
                )
                if result.status not in {
                    "completed",
                    "cancelled",
                    "needs_review",
                    "stale_context",
                }:
                    raise RuntimeError(result.error_message or "AgentRun did not complete")
            elif job.kind == "research_command":
                if self.research_command_service is None:
                    raise RuntimeError("ResearchCommand orchestration is not configured")
                self.research_command_service.execute_claimed(job)
            elif job.kind == "projection_rebuild":
                jobs = ProjectionRebuildJobs(self.repository)
                if job.payload.get("version") != "projection-rebuild-v1":
                    raise ValueError("Unsupported projection rebuild request version")
                request = RebuildRequest.model_validate({
                    k: job.payload[k] for k in ("target", "collection_slug", "confirmed")
                })
                rebuild = KnowledgeProjectionRebuildService(
                    self.repository,
                    indexer=self.ingestion_service.indexer,
                    projector=self.ingestion_service.projector,
                    vault_exporter=self.ingestion_service.vault_exporter,
                )
                result = rebuild.rebuild(
                    target=request.target, collection_slug=request.collection_slug,
                    assert_active=lambda: jobs.assert_active(job),
                )
                jobs.complete(job, result)
                return
            elif job.kind == "collection_sync":
                self.ingestion_service.sync_collections(
                    list(job.payload.get("collection_slugs", []))
                )
            else:
                raise ValueError(f"Unsupported job kind: {job.kind}")
            self.repository.jobs.complete_job(
                job.id,
                expected_attempt=job.attempts,
                expected_owner=job.lease_owner,
            )
        except Exception as exc:
            try:
                if job.kind == "ingestion":
                    self.ingestion_service.fail_claimed(job, str(exc))
                elif job.kind == "report":
                    self.report_service.fail_claimed(
                        job.resource_id,
                        job.id,
                        job.attempts,
                        str(exc),
                        expected_owner=job.lease_owner,
                    )
                else:
                    self.repository.jobs.fail_job(
                        job.id,
                        str(exc),
                        expected_attempt=job.attempts,
                        expected_owner=job.lease_owner,
                    )
            except Exception:
                self.repository.jobs.fail_job(
                    job.id,
                    str(exc),
                    expected_attempt=job.attempts,
                    expected_owner=job.lease_owner,
                )
        finally:
            if self.research_command_service is not None and job.kind in {
                "report",
                "agent_run",
            }:
                try:
                    self.research_command_service.refresh_for_target(
                        job.kind,
                        job.resource_id,
                    )
                except Exception:
                    # Target completion remains authoritative. GET command is a repair path.
                    pass
            stopped.set()
            heartbeat.join(timeout=1.0)
            self._heartbeat_executor()

    def _renew_job(self, job: KnowledgeJob, stopped: threading.Event) -> None:
        interval = self._heartbeat_interval()
        while not stopped.wait(interval):
            try:
                renewed = self.repository.jobs.renew_job_lease(
                    job.id,
                    expected_attempt=job.attempts,
                    lease_seconds=self.lease_seconds,
                    expected_owner=job.lease_owner,
                )
                self._heartbeat_executor(job.id)
            except Exception:
                continue
            if not renewed:
                return

    def _execute_projection_with_heartbeat(self, projection: ProjectionEvent) -> None:
        stopped = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_projection,
            args=(projection, stopped),
            daemon=True,
            name=f"worker-projection-lease-{projection.id}",
        )
        self._heartbeat_executor(projection.id)
        heartbeat.start()
        try:
            self.ingestion_service.process_projection(projection)
        except Exception:
            pass
        finally:
            stopped.set()
            heartbeat.join(timeout=1.0)
            self._heartbeat_executor()

    def _renew_projection(self, projection: ProjectionEvent, stopped: threading.Event) -> None:
        interval = self._heartbeat_interval()
        while not stopped.wait(interval):
            if not projection.lease_owner:
                return
            try:
                renewed = self.repository.renew_projection_lease(
                    projection.id,
                    expected_attempt=projection.attempts,
                    expected_owner=projection.lease_owner,
                    lease_seconds=self.lease_seconds,
                )
                self._heartbeat_executor(projection.id)
            except Exception:
                continue
            if not renewed:
                return

    def _heartbeat_executor(self, current_job_id: str | None = None) -> None:
        self.repository.jobs.upsert_executor_heartbeat(
            self.worker_id,
            role="worker",
            version=self.version,
            started_at=self.started_at,
            current_job_id=current_job_id,
            metadata={"concurrency": 1},
        )

    def _heartbeat_interval(self) -> float:
        return max(0.05, min(30.0, max(1, self.lease_seconds) / 3))

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        self.repository.recover_running_work()
        while True:
            if not self.run_once():
                time.sleep(poll_seconds)


def build_worker() -> KnowledgeWorker:
    settings = get_settings()
    repository = KnowledgeRepository(settings.knowledge_db_path)
    ingestion_service = KnowledgeIngestionService(repository, settings=settings)
    report_service = KnowledgeReportService(repository, settings=settings)
    agent_service = AgentRunService(repository, settings=settings)
    agent_runtime = AgentRuntime(
        agent_service,
        checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
    )
    research_commands = ResearchCommandService(
        repository,
        report_service=report_service,
        agent_service=agent_service,
    )
    return KnowledgeWorker(
        repository,
        ingestion_service=ingestion_service,
        report_service=report_service,
        agent_runtime=agent_runtime,
        research_command_service=research_commands,
        lease_seconds=settings.knowledge_worker_lease_seconds,
    )


def main() -> None:
    settings = get_settings()
    build_worker().run_forever(settings.knowledge_worker_poll_seconds)


if __name__ == "__main__":
    main()
