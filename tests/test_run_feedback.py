"""Feedback lifecycle through real SQLite, ContextBuilder, API and Worker boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.errors import AgentRunConflictError
from app.agent.feedback_models import (
    FeedbackDecision,
    FeedbackRecheck,
    FeedbackRequest,
    FeedbackRerunRequest,
)
from app.api.routers.agent import build_agent_router
from app.context.service import ContextConflictError
from app.knowledge.repository import KnowledgeRepository
from tests.test_research_workflow import (
    _analysis,
    _draft,
    _plan,
    _research_request,
    _research_stack,
)


@pytest.fixture
def stack(tmp_path):
    values = _research_stack(tmp_path, [])
    repo, service, worker, project, task, package, bundle, llm = values
    llm.responses = [
        json.dumps(x)
        for x in (
            _analysis(),
            _plan(),
            _draft(
                bundle.claim.claim_id, bundle.evidence[0].evidence_id, valid=True, proposal=False
            ),
        )
    ]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))
    assert worker.run_once()
    assert service.get_run(run.id).status == "completed"
    return (*values, run)


def submit(stack, key="issue-1"):
    _, service, _, _, _, _, bundle, _, run = stack
    return service.feedback.create(
        run.id,
        FeedbackRequest(
            idempotency_key=key,
            category="incomplete",
            reporter="fixture reviewer",
            note="State the limitation explicitly.",
            finding_index=1,
            evidence_ids=[bundle.evidence[0].evidence_id],
        ),
    )


def accept(stack, feedback):
    service, run = stack[1], stack[-1]
    return service.feedback.decide(
        run.id,
        feedback["id"],
        FeedbackDecision(
            expected_revision=feedback["revision"],
            decision="accepted",
            reviewer="fixture reviewer",
            note="Confirmed missing qualification; revise task before a new run.",
        ),
    )


def rerun_request(revision=2, key="rerun-1"):
    return FeedbackRerunRequest(
        expected_revision=revision,
        idempotency_key=key,
        resolution_note="Task now explicitly requires the evidence limitation.",
    )


def test_explicit_rerun_capacity_preserves_parent_and_idempotency(stack):
    from pydantic import ValidationError

    service, parent = stack[1], stack[-1]
    feedback = accept(stack, submit(stack))
    request = FeedbackRerunRequest(**rerun_request().model_dump(exclude_none=True),
                                   token_budget=1048576, context_max_tokens=262144)
    child = service.feedback.rerun(parent.id, feedback["id"], request)
    assert child.token_budget == 1048576
    assert service.load_context_snapshot(child.id).token_usage.budget == 262144
    assert service.load_context_snapshot(parent.id).model_dump() == stack[5].model_dump()
    assert service.get_run(parent.id).token_budget == parent.token_budget
    assert service.feedback.rerun(parent.id, feedback["id"], request).id == child.id
    assert service.feedback.list(parent.id)["links"][0]["request"]["token_budget"] == 1048576
    with pytest.raises(AgentRunConflictError, match="Conflicting duplicate"):
        service.feedback.rerun(parent.id, feedback["id"], request.model_copy(
            update={"token_budget": 32000}))
    with pytest.raises(AgentRunConflictError, match="Conflicting duplicate"):
        service.feedback.rerun(parent.id, feedback["id"], request.model_copy(
            update={"context_max_tokens": 16000}))
    for invalid in (0, 1048577):
        with pytest.raises(ValidationError):
            FeedbackRerunRequest(**{**request.model_dump(), "token_budget": invalid})


def test_rerun_rejects_context_larger_than_run_without_creating_records(stack):
    from pydantic import ValidationError

    service, parent = stack[1], stack[-1]
    feedback = accept(stack, submit(stack))
    before = service.feedback.list(parent.id)
    with pytest.raises(ValidationError, match="Context limit"):
        service.feedback.rerun(parent.id, feedback["id"], FeedbackRerunRequest(
            **rerun_request().model_dump(exclude_none=True), context_max_tokens=262144))
    assert service.feedback.list(parent.id) == before


def test_legacy_rerun_request_without_capacity_remains_replayable(stack):
    service, parent = stack[1], stack[-1]
    feedback = accept(stack, submit(stack))
    request = rerun_request()
    child = service.feedback.rerun(parent.id, feedback["id"], request)
    assert child.token_budget == parent.token_budget
    assert "token_budget" not in service.feedback.list(parent.id)["links"][0]["request"]
    assert service.feedback.rerun(parent.id, feedback["id"], request).id == child.id


def test_feedback_revision_worker_recheck_preserves_history(stack, tmp_path):
    repo, service, worker, _, task, package, bundle, llm, run = stack
    old_output = service.repository.get_output(run.id).model_dump()
    feedback = submit(stack)
    assert feedback["anchor"]["evidence"][0]["source_version"] == bundle.sources[0].version
    feedback = accept(stack, feedback)
    current_task = repo.memory_repository.get_workspace_task(task.id)
    repo.memory_repository.update_workspace_task(
        task.id,
        expected_revision=current_task.revision,
        goal="Summarize the evidence and explicitly qualify the supplied snapshot limitation.",
    )
    child = service.feedback.rerun(run.id, feedback["id"], rerun_request())
    child_package = service.context_builder.snapshot_repository.get(child.context_snapshot_id)
    assert child.context_snapshot_id != run.context_snapshot_id
    assert child_package.task.goal != package.task.goal
    draft = _draft(
        bundle.claim.claim_id, bundle.evidence[0].evidence_id, valid=True, proposal=False
    )
    draft["findings"][0]["assertion"] = "The traceable chain is limited to this supplied snapshot."
    llm.responses = [json.dumps(x) for x in (_analysis(), _plan(), draft)]
    assert worker.run_once()
    link = service.feedback.list(run.id)["links"][0]
    assert link["child_status"] == "completed"
    assert link["child_artifact_id"] != old_output["structured"]["artifact_id"]
    assert service.feedback.list(child.id)["links"][0]["parent_run_id"] == run.id
    check = FeedbackRecheck(
        decision="resolved",
        reviewer="fixture reviewer",
        note="Revised result includes the required limitation.",
    )
    service.feedback.recheck(run.id, link["id"], check)
    assert service.feedback.recheck(run.id, link["id"], check)["recheck"] == check.model_dump()
    assert service.feedback.rerun(run.id, feedback["id"], rerun_request()).id == child.id
    assert service.repository.get_output(run.id).model_dump() == old_output
    assert service.context_builder.snapshot_repository.get(package.snapshot_id) == package
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 2
    export = service.feedback.export_candidate(run.id, feedback["id"], "research-dev-next")
    assert export["split"] == "dev" and export["gold_label"] is None
    assert export["automatic_import"] is False
    (tmp_path / "feedback-loop.json").write_text(
        json.dumps(
            {
                "mode": "deterministic_fixture_not_quality_evaluation",
                "original_output": old_output,
                "original_snapshot": package.model_dump(mode="json"),
                "history": service.feedback.list(run.id),
                "child_snapshot": child_package.model_dump(mode="json"),
                "child_output": service.repository.get_output(child.id).model_dump(),
                "dev_candidate": export,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_review_required_duplicates_and_foreign_anchors(stack):
    service, run = stack[1], stack[-1]
    feedback = submit(stack)
    assert submit(stack)["id"] == feedback["id"]
    with pytest.raises(AgentRunConflictError):
        service.feedback.rerun(run.id, feedback["id"], rerun_request(1))
    with pytest.raises(AgentRunConflictError):
        service.feedback.export_candidate(run.id, feedback["id"], "dev-next")
    for changes in (
        {"note": "conflicting duplicate"},
        {"evidence_ids": ["foreign"]},
        {"finding_index": 99},
    ):
        request = {**feedback["request"], **changes}
        if "note" not in changes:
            request["idempotency_key"] = str(changes)
        with pytest.raises(AgentRunConflictError):
            service.feedback.create(run.id, FeedbackRequest(**request))
    with pytest.raises(KeyError):
        service.feedback.decide(
            "other-run",
            feedback["id"],
            FeedbackDecision(
                expected_revision=1,
                decision="accepted",
                reviewer="human",
                note="Checked",
            ),
        )
    reject = FeedbackDecision(
        expected_revision=1, decision="rejected", reviewer="human", note="Not confirmed."
    )
    service.feedback.decide(run.id, feedback["id"], reject)
    assert service.feedback.decide(run.id, feedback["id"], reject)["status"] == "rejected"
    with pytest.raises(AgentRunConflictError):
        accept(stack, feedback)
    with pytest.raises(AgentRunConflictError):
        service.feedback.export_candidate(run.id, feedback["id"], "dev-next")


def test_concurrent_rerun_has_one_child_job_and_history(stack):
    repo, service, *_, run = stack
    feedback = accept(stack, submit(stack))
    with ThreadPoolExecutor(max_workers=2) as pool:
        children = list(
            pool.map(
                lambda _: service.feedback.rerun(run.id, feedback["id"], rerun_request()), range(2)
            )
        )
    assert children[0].id == children[1].id
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_rechecks").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_jobs WHERE resource_id = ?", (children[0].id,)
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(AgentRunConflictError):
        service.feedback.rerun(run.id, feedback["id"], rerun_request(3, "second-key"))


def test_queue_failure_rolls_back_child_and_link_and_can_retry(stack, monkeypatch):
    repo, service, *_, run = stack
    feedback = accept(stack, submit(stack))
    original = repo.jobs.enqueue_job_tx

    def fail(*args, **kwargs):
        raise RuntimeError("injected queue failure")

    monkeypatch.setattr(repo.jobs, "enqueue_job_tx", fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.feedback.rerun(run.id, feedback["id"], rerun_request())
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_run_rechecks").fetchone()[0] == 0
    monkeypatch.setattr(repo.jobs, "enqueue_job_tx", original)
    assert service.feedback.rerun(run.id, feedback["id"], rerun_request()).status == "queued"


@pytest.mark.parametrize("change", ["task", "scope", "source", "parent", "feedback"])
def test_changes_during_preparation_prevent_child_commit(stack, monkeypatch, change):
    repo, service, _, project, task, _, bundle, _, run = stack
    feedback = accept(stack, submit(stack))
    original = service._resolve_context

    def race(*args):
        context = original(*args)
        with repo._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if change == "task":
                connection.execute(
                    "UPDATE workspace_tasks SET revision = revision + 1 WHERE id = ?", (task.id,)
                )
            elif change == "scope":
                connection.execute(
                    "DELETE FROM project_knowledge_scopes WHERE project_id = ?", (project.id,)
                )
            elif change == "source":
                connection.execute(
                    "UPDATE sources SET version = 'new-version' WHERE id = ?",
                    (bundle.sources[0].source_id,),
                )
            elif change == "parent":
                connection.execute(
                    "UPDATE agent_runs SET revision = revision + 1 WHERE id = ?", (run.id,)
                )
            else:
                connection.execute(
                    "UPDATE agent_run_feedback SET revision = revision + 1 WHERE id = ?",
                    (feedback["id"],),
                )
        return context

    monkeypatch.setattr(service, "_resolve_context", race)
    with pytest.raises((AgentRunConflictError, ContextConflictError)):
        service.feedback.rerun(run.id, feedback["id"], rerun_request())
    assert service.feedback.list(run.id)["links"] == []
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1


def test_failed_child_requires_unresolved_recheck_before_new_attempt(stack):
    _, service, worker, _, _, _, _, llm, run = stack
    feedback = accept(stack, submit(stack))
    child = service.feedback.rerun(run.id, feedback["id"], rerun_request())
    link = service.feedback.list(run.id)["links"][0]
    resolved = FeedbackRecheck(decision="resolved", reviewer="human", note="Checked.")
    with pytest.raises(AgentRunConflictError):
        service.feedback.recheck(run.id, link["id"], resolved)
    llm.responses = [RuntimeError("scripted provider failure")]
    assert worker.run_once()
    assert service.get_run(child.id).status == "failed"
    with pytest.raises(AgentRunConflictError):
        service.feedback.recheck(run.id, link["id"], resolved)
    service.feedback.recheck(
        run.id,
        link["id"],
        FeedbackRecheck(
            decision="unresolved",
            reviewer="human",
            note="Provider failed; preserve failed attempt.",
        ),
    )
    second = service.feedback.rerun(run.id, feedback["id"], rerun_request(4, "rerun-2"))
    assert second.id != child.id
    assert len(service.feedback.list(run.id)["links"]) == 2


def test_existing_review_endpoint_rerun_is_idempotent_after_partial_failure(stack, monkeypatch):
    _, service, _, _, _, _, _, _, run = stack
    # Deterministic fixture for a validation stop without any published output.
    with service.repository._connect() as connection:
        connection.execute("UPDATE agent_runs SET status = 'needs_review' WHERE id = ?", (run.id,))
    decide = service.feedback.decide
    monkeypatch.setattr(
        service.feedback,
        "decide",
        lambda *args: (_ for _ in ()).throw(RuntimeError("interrupted before review decision")),
    )
    with pytest.raises(RuntimeError):
        service.review_run(run.id, action="rerun")
    monkeypatch.setattr(service.feedback, "decide", decide)
    child = service.review_run(run.id, action="rerun")
    assert service.review_run(run.id, action="rerun").id == child.id


def test_feedback_api_validation_review_and_migration_preserve_history(stack):
    repo, service, _, _, _, _, _, _, run = stack
    app = FastAPI()
    app.include_router(build_agent_router(None, service, None))
    with TestClient(app) as client:
        path = f"/api/agent-runs/{run.id}/feedback"
        invalid = client.post(
            path,
            json={"idempotency_key": "a", "category": "other", "note": "   ", "reporter": "human"},
        )
        assert invalid.status_code == 422
        created = client.post(
            path,
            json={
                "idempotency_key": "a",
                "category": "other",
                "note": "Check limitation",
                "reporter": "human",
            },
        )
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert (
            client.post(
                f"{path}/{identifier}/review",
                json={
                    "expected_revision": 1,
                    "decision": "accepted",
                    "reviewer": "human",
                    "note": "Confirmed",
                },
            ).status_code
            == 200
        )
        child = client.post(f"{path}/{identifier}/rerun", json=rerun_request().model_dump())
        assert child.status_code == 202
        assert client.get(path).json()["links"][0]["child_run_id"] == child.json()["id"]
        export = client.post(
            f"{path}/{identifier}/dev-candidate",
            json={
                "target_dev_version": "next-dev",
                "split": "holdout",
            },
        )
        assert export.status_code == 422
    reopened = KnowledgeRepository(repo.path)
    assert reopened.schema_version() == 20
    assert service.feedback.list(run.id)["feedback"][0]["decision"]["reviewer"] == "human"


def test_upgrade_from_v18_preserves_original_output_snapshot_and_job(stack):
    repo, service, _, _, _, package, _, _, run = stack
    output = service.repository.get_output(run.id).model_dump()
    with repo._connect() as connection:
        before_jobs = [dict(row) for row in connection.execute("SELECT * FROM knowledge_jobs")]
        # Only this isolated fixture is reduced to its previous structural version.
        connection.execute("DROP TABLE agent_run_rechecks")
        connection.execute("DROP TABLE agent_run_feedback")
        connection.execute("DROP TABLE research_generation_attempts")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (19, 20)")
    upgraded = KnowledgeRepository(repo.path)
    assert upgraded.schema_version() == 20
    assert service.repository.get_output(run.id).model_dump() == output
    assert service.context_builder.snapshot_repository.get(package.snapshot_id) == package
    with upgraded._connect() as connection:
        assert [
            dict(row) for row in connection.execute("SELECT * FROM knowledge_jobs")
        ] == before_jobs
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert submit(stack)["status"] == "pending"


def test_research_rerun_refuses_no_scope_and_mutated_original_run(stack):
    repo, service, _, project, _, _, _, _, run = stack
    feedback = accept(stack, submit(stack))
    with repo._connect() as connection:
        connection.execute(
            "DELETE FROM project_knowledge_scopes WHERE project_id = ?", (project.id,)
        )
    with pytest.raises(AgentRunConflictError, match="scope"):
        service.feedback.rerun(run.id, feedback["id"], rerun_request())
    with repo._connect() as connection:
        connection.execute("UPDATE agent_runs SET revision = revision + 1 WHERE id = ?", (run.id,))
    with pytest.raises(AgentRunConflictError, match="Original run changed"):
        service.feedback.rerun(run.id, feedback["id"], rerun_request())


def test_link_failure_after_enqueue_rolls_back_every_record(stack, monkeypatch):
    repo, service, *_, run = stack
    feedback = accept(stack, submit(stack))
    original = service.feedback.link_rerun_tx

    def fail(*args):
        original(*args)
        raise RuntimeError("injected after link and job writes")

    monkeypatch.setattr(service.feedback, "link_rerun_tx", fail)
    with pytest.raises(RuntimeError, match="injected"):
        service.feedback.rerun(run.id, feedback["id"], rerun_request())
    assert service.feedback.list(run.id)["links"] == []
    assert service.feedback.list(run.id)["feedback"][0]["revision"] == 2
    with repo._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_jobs").fetchone()[0] == 1


def test_tampered_output_is_not_accepted_as_feedback_anchor(stack):
    repo, service, *_, run = stack
    with repo._connect() as connection:
        connection.execute(
            "UPDATE agent_run_outputs SET rendered_text = 'tampered' WHERE run_id = ?", (run.id,)
        )
    with pytest.raises(AgentRunConflictError, match="integrity"):
        submit(stack)
    assert service.feedback.list(run.id)["feedback"] == []
