from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.knowledge.rebuild import KnowledgeProjectionRebuildService, ProjectionRebuildResult
from app.knowledge.rebuild_jobs import ProjectionRebuildJobs, RebuildRequest
from app.worker import KnowledgeWorker
from tests.test_runtime_api import _stack


def _submit(client, key="maintenance-request-1", **updates):
    return client.post(
        "/api/v1/runtime/rebuilds",
        headers={"Idempotency-Key": key},
        json={
            "target": "qdrant",
            "collection_slug": None,
            "confirmed": True,
            **updates,
        },
    )


def test_rebuild_api_only_queues_valid_confirmed_idempotent_requests(tmp_path):
    repository, service, _, app = _stack(tmp_path)
    service.indexer = SimpleNamespace(
        replace=lambda *args, **kwargs: pytest.fail("API wrote index")
    )
    client = TestClient(app)
    assert _submit(client, confirmed=False).status_code == 422
    assert _submit(client, target="unknown").status_code == 422
    assert _submit(client, collection_slug="missing").status_code == 404
    first = _submit(client)
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert _submit(client).json()["id"] == first.json()["id"]
    assert _submit(client, target="all").status_code == 409
    assert _submit(client, key="maintenance-request-2").status_code == 409
    work = client.get("/api/v1/runtime/work?kind=projection_rebuild").json()["items"]
    assert len(work) == 1
    assert work[0]["detail_route"] == "/runtime?rebuild=" + first.json()["id"]
    assert repository.jobs.get_job(first.json()["id"]).attempts == 0


def test_worker_failure_retry_and_result_survive_reader_restart(tmp_path):
    repository, service, _, app = _stack(tmp_path)
    client = TestClient(app)
    calls = []

    def replace(chunks, *, collection_slug=None):
        # An external adapter must run outside the enqueue/commit transaction.
        with repository._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
        calls.append(collection_slug)
        if len(calls) == 1:
            raise RuntimeError("index unavailable")

    service.indexer = SimpleNamespace(replace=replace)
    worker = KnowledgeWorker(repository, ingestion_service=service, report_service=None)
    job_id = _submit(client).json()["id"]
    assert worker.run_once()
    assert ProjectionRebuildJobs(repository).get(job_id).status == "failed"
    assert _submit(client).json()["status"] == "failed"  # Replayed POST is not a retry.
    assert client.post(f"/api/v1/runtime/rebuilds/{job_id}/retry").status_code == 202
    assert client.post(f"/api/v1/runtime/rebuilds/{job_id}/retry").status_code == 409
    assert worker.run_once()
    result = client.get(f"/api/v1/runtime/rebuilds/{job_id}").json()
    assert result["status"] == "completed"
    assert result["attempts"] == 2
    assert result["payload"]["result"]["chunks"] == 0
    assert client.post(f"/api/v1/runtime/rebuilds/{job_id}/retry").status_code == 409
    assert calls == [None, None]


def test_lease_loss_blocks_following_adapter_and_stale_result(tmp_path):
    repository, service, _, app = _stack(tmp_path)
    jobs = ProjectionRebuildJobs(repository)
    job_id = _submit(TestClient(app), target="all").json()["id"]
    job = repository.jobs.claim_job(owner_id="first")
    assert job.id == job_id

    def take_over(*args, **kwargs):
        with repository._connect() as connection:
            connection.execute(
                "UPDATE knowledge_jobs SET lease_until = '2000-01-01' WHERE id = ?", (job_id,)
            )
        repository.jobs.claim_job(owner_id="second")

    rebuild = KnowledgeProjectionRebuildService(
        repository,
        indexer=SimpleNamespace(replace=take_over),
        projector=SimpleNamespace(replace_all=lambda *args: pytest.fail("stale graph write")),
        vault_exporter=service.vault_exporter,
    )
    with pytest.raises(ValueError, match="执行权"):
        rebuild.rebuild(target="all", assert_active=lambda: jobs.assert_active(job))
    with pytest.raises(ValueError, match="执行权"):
        jobs.complete(job, ProjectionRebuildResult(target="all"))
    assert jobs.get(job_id).lease_owner == "second"
    assert "result" not in jobs.get(job_id).payload


def test_unknown_request_version_fails_without_adapter_call(tmp_path):
    repository, service, _, app = _stack(tmp_path)
    job_id = _submit(TestClient(app)).json()["id"]
    with repository._connect() as connection:
        connection.execute(
            "UPDATE knowledge_jobs SET payload_json = ? WHERE id = ?",
            ('{"version":"future"}', job_id),
        )
    service.indexer = SimpleNamespace(
        replace=lambda *args, **kwargs: pytest.fail("unknown version")
    )
    KnowledgeWorker(repository, ingestion_service=service, report_service=None).run_once()
    assert ProjectionRebuildJobs(repository).get(job_id).status == "failed"


def test_rebuild_submission_serializes_and_rejects_recent_old_worker(tmp_path):
    repository, _, _, app = _stack(tmp_path)
    now = datetime.now(UTC).isoformat()
    repository.jobs.upsert_executor_heartbeat(
        "old-worker", role="worker", version="unified-worker-v1", started_at=now
    )
    assert _submit(TestClient(app)).status_code == 409
    jobs = ProjectionRebuildJobs(repository)

    def submit(key):
        try:
            return jobs.submit(RebuildRequest(target="qdrant", confirmed=True), key).id
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["concurrent-1", "concurrent-2"]))
    assert sum(result is not None for result in results) == 1
