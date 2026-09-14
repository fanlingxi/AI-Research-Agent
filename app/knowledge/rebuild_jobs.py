"""Durable projection maintenance requests on the existing Worker queue."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.knowledge.rebuild import ProjectionRebuildResult, ProjectionTarget
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import KnowledgeJob


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: ProjectionTarget
    collection_slug: str | None = None
    confirmed: Literal[True]


class ProjectionRebuildJobs:
    def __init__(self, repository: KnowledgeRepository):
        self.repository = repository

    def get(self, job_id: str) -> KnowledgeJob:
        job = self.repository.jobs.get_job(job_id)
        if job.kind != "projection_rebuild":
            raise KeyError(job_id)
        return job

    def submit(self, request: RebuildRequest, key: str) -> KnowledgeJob:
        resource_id = "rebuild-" + hashlib.sha256(key.encode()).hexdigest()
        payload = {**request.model_dump(), "version": "projection-rebuild-v1"}
        with self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE kind = 'projection_rebuild' "
                "AND resource_id = ?",
                (resource_id,),
            ).fetchone()
            if existing:
                previous = json.loads(existing["payload_json"])
                if any(previous.get(k) != v for k, v in payload.items()):
                    raise ValueError("同一请求标识不能用于不同的重建参数。")
                job_id = existing["id"]
            else:
                self._check_scope(connection, request.collection_slug)
                self._check_idle(connection)
                job_id = self.repository.jobs.enqueue_job_tx(
                    connection,
                    kind="projection_rebuild",
                    resource_id=resource_id,
                    payload=payload,
                )
        return self.get(job_id)

    @staticmethod
    def _check_scope(connection, slug):
        if (
            slug is not None
            and not connection.execute(
                "SELECT 1 FROM knowledge_collections WHERE slug = ?",
                (slug,),
            ).fetchone()
        ):
            raise KeyError(slug)

    @staticmethod
    def _check_idle(connection):
        if connection.execute(
            "SELECT 1 FROM knowledge_jobs WHERE kind = 'projection_rebuild' "
            "AND status IN ('queued', 'running')",
        ).fetchone():
            raise ValueError("已有投影重建排队或执行中，请等待完成。")

    def retry(self, job_id: str) -> KnowledgeJob:
        with self.repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM knowledge_jobs WHERE id = ? AND kind = 'projection_rebuild'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "failed":
                raise ValueError("仅失败的投影重建可以重试。")
            payload = json.loads(row["payload_json"])
            self._check_scope(connection, payload.get("collection_slug"))
            self._check_idle(connection)
            self.repository.jobs.enqueue_job_tx(
                connection,
                kind="projection_rebuild",
                resource_id=row["resource_id"],
                payload=payload,
                force_requeue=True,
            )
        return self.get(job_id)

    def assert_active(self, job: KnowledgeJob) -> None:
        current = self.get(job.id)
        if (
            current.status != "running"
            or current.attempts != job.attempts
            or not job.lease_owner
            or current.lease_owner != job.lease_owner
            or not current.lease_until
            or datetime.fromisoformat(current.lease_until) <= datetime.now(UTC)
        ):
            raise ValueError("重建执行权已失效，停止后续操作。")

    def complete(self, job: KnowledgeJob, result: ProjectionRebuildResult) -> None:
        now = datetime.now(UTC).isoformat()
        payload = {**job.payload, "result": result.model_dump()}
        with self.repository._connect() as connection:
            updated = connection.execute(
                "UPDATE knowledge_jobs SET status = 'completed', payload_json = ?, "
                "lease_until = NULL, lease_owner = NULL, last_error = NULL, updated_at = ? "
                "WHERE id = ? AND kind = 'projection_rebuild' AND status = 'running' "
                "AND attempts = ? AND lease_owner = ? AND lease_until > ?",
                (
                    json.dumps(payload, ensure_ascii=False),
                    now,
                    job.id,
                    job.attempts,
                    job.lease_owner,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("重建执行权已失效，结果未提交。")
