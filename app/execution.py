from __future__ import annotations

from dataclasses import dataclass

from app.knowledge.schemas import KnowledgeJob


@dataclass(frozen=True)
class ExecutionFence:
    """The durable claim token required for one executor to mutate a resource."""

    job_id: str
    kind: str
    resource_id: str
    attempt: int
    owner_id: str

    @classmethod
    def from_job(cls, job: KnowledgeJob) -> ExecutionFence:
        if not job.lease_owner:
            raise ValueError(f"Claimed job {job.id} does not have a lease owner.")
        return cls(
            job_id=job.id,
            kind=job.kind,
            resource_id=job.resource_id,
            attempt=job.attempts,
            owner_id=job.lease_owner,
        )
