from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.errors import ResearchLLMRequiredError
from app.agent.models import AgentRunCreateRequest
from app.agent.runtime import RESEARCH_CHECKPOINT_NAMESPACE, AgentRuntime
from app.agent.service import AgentRunService
from app.agent.tools import build_research_tool_registry
from app.api.main import create_app
from app.config.settings import Settings
from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService
from app.knowledge.reports import KnowledgeReportService
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.schemas import EvidenceSpan, PublishedEntity
from app.knowledge.service import KnowledgeIngestionService
from app.llms.provider import MockLLMClient
from app.worker import KnowledgeWorker
from tests.core_fixtures import persist_evidence_chunk


class _ScriptedLLM:
    provider_name = "test"

    def __init__(self, responses: list[dict], *, usage: dict[str, int] | None = None) -> None:
        self.responses: list[str | Exception] = [
            json.dumps(response, ensure_ascii=False) for response in responses
        ]
        self.calls: list[str] = []
        self.last_usage: dict[str, int] = {}
        self.usage = usage or {"input_tokens": 11, "output_tokens": 7}

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        self.calls.append(prompt)
        self.last_usage = dict(self.usage)
        if not self.responses:
            raise AssertionError("Unexpected research LLM call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _research_stack(tmp_path, responses: list[dict]):
    settings = Settings(
        knowledge_db_path=str(tmp_path / "knowledge.db"),
        knowledge_vault_path=str(tmp_path / "vault"),
        agent_checkpoint_path=str(tmp_path / "agent_checkpoints.db"),
    )
    repository = KnowledgeRepository(settings.knowledge_db_path)
    collection = repository.create_collection("Research Scope")
    ingestion = repository.create_ingestion(
        collection=collection.slug,
        sources=["research-fixture.pdf"],
        pdf_max_pages=2,
        enqueue=False,
    )
    repository.update_ingestion(ingestion.id, status="needs_review")
    evidence = EvidenceSpan(
        paper_id="research-paper",
        chunk_id="research-paper:page:1:chunk:0",
        page_start=1,
        page_end=1,
        quote="A governed ContextSnapshot retains a traceable claim-to-evidence chain.",
    )
    persist_evidence_chunk(repository, ingestion_id=ingestion.id, evidence=evidence)
    entity = PublishedEntity(
        id="research-entity",
        name="Governed ContextSnapshot",
        type="Method",
        summary="A context package constrained by scoped formal evidence.",
        evidence=[evidence],
        topic_slugs=[collection.slug],
        collection_slugs=[collection.slug],
    )
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO published_entities (
                id, normalized_name, entity_type, payload_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (entity.id, entity.name.casefold(), entity.type, entity.model_dump_json()),
        )
        connection.execute(
            """
            INSERT INTO collection_memberships
                (aggregate_type, aggregate_id, ingestion_id, collection_slug, created_at)
            VALUES ('entity', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (entity.id, ingestion.id, collection.slug),
        )
        repository.core_repository.synchronize_published_entity_tx(connection, entity)

    memory = repository.memory_repository
    project = memory.create_project(
        name="Research Project",
        goal="Produce a cited research report.",
        domain="research",
        metadata={},
    )
    memory.replace_project_knowledge_scopes(
        project.id, [collection.slug], expected_project_revision=project.revision
    )
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Assess governed context",
        goal="Summarize the evidence-grounded workflow.",
        priority="high",
        metadata={"expected_output": "A cited research report."},
    )
    builder = ContextBuilderService(repository)
    package = builder.build_context(
        ContextBuildRequest(task_id=task.id, project_id=project.id, max_tokens=6000)
    )
    assert package.knowledge.claim_bundles

    llm = _ScriptedLLM(responses)
    service = AgentRunService(
        repository,
        context_builder=builder,
        settings=settings,
        llm=llm,
    )
    runtime = AgentRuntime(
        service,
        checkpoint_factory=AgentCheckpointFactory(settings.agent_checkpoint_path),
    )
    worker = KnowledgeWorker(
        repository,
        ingestion_service=KnowledgeIngestionService(repository, settings=settings),
        report_service=KnowledgeReportService(
            repository, settings=settings, require_live_llm=False
        ),
        agent_runtime=runtime,
    )
    bundle = package.knowledge.claim_bundles[0]
    return repository, service, worker, project, task, package, bundle, llm


def _analysis() -> dict:
    return {
        "research_question": "What does the scoped evidence establish?",
        "intended_output": "A concise cited report.",
        "constraints": ["Use only the ContextSnapshot."],
    }


def _plan() -> dict:
    return {
        "steps": ["Read immutable research input.", "Write cited findings."],
        "tool_sequence": ["context.research_input"],
    }


def _draft(bundle_id: str, evidence_id: str, *, valid: bool, proposal: bool = True) -> dict:
    cited_id = evidence_id if valid else "invented-evidence"
    return {
        "title": "Governed Context Research",
        "executive_summary": "The snapshot preserves an evidence-grounded research boundary.",
        "findings": [
            {
                "claim_bundle_id": bundle_id,
                "assertion": "The formal claim is supported by a locatable evidence chain.",
                "evidence_ids": [cited_id],
            }
        ],
        "limitations": ["The conclusion is limited to the supplied snapshot."],
        "markdown": f"## Finding\nThe claim is evidence-grounded. [cite:{cited_id}]",
        "memory_proposal": (
            {
                "summary": "Keep evidence scope explicit.",
                "rationale": "The cited result depends on immutable scoped context.",
                "impact": "Future tasks should preserve the same boundary.",
            }
            if proposal
            else None
        ),
    }


def _research_request(snapshot_id: str, *, propose: bool = False) -> AgentRunCreateRequest:
    return AgentRunCreateRequest(
        workflow="research",
        context_snapshot_id=snapshot_id,
        create_memory_proposal=propose,
        max_steps=10,
        max_tool_calls=1,
        token_budget=6000,
    )


def test_research_workflow_finalizes_cited_output_artifact_and_proposal_atomically(
    tmp_path,
) -> None:
    placeholder = [{}, {}, {}]
    repository, service, worker, project, task, package, bundle, llm = _research_stack(
        tmp_path, placeholder
    )
    evidence_id = bundle.evidence[0].evidence_id
    llm.responses = [
        json.dumps(_analysis(), ensure_ascii=False),
        json.dumps(_plan(), ensure_ascii=False),
        json.dumps(_draft(bundle.claim.claim_id, evidence_id, valid=True), ensure_ascii=False),
    ]
    run = service.create_run(
        project.id, task.id, _research_request(package.snapshot_id, propose=True)
    )

    assert worker.run_once()
    completed = service.get_run(run.id)
    output = service.repository.get_output(run.id)
    artifact = repository.memory_repository.get_artifact(output.structured["artifact_id"])
    proposal = repository.memory_repository.get_proposal(output.structured["memory_proposal_id"])
    tool_call = service.repository.list_tool_calls(run.id)[0]
    node_events = service.repository.list_events(run.id)

    assert completed.status == "completed"
    assert output.output_type == "research_report"
    assert output.validation["passed"] is True
    assert artifact.status == "ready"
    assert artifact.reference == f"agent-run-output:{output.id}"
    assert not Path(artifact.reference).exists()
    assert proposal.status == "proposed"
    assert proposal.proposal_type == "decision_create"
    assert tool_call.tool_name == "context.research_input"
    assert "traceable claim-to-evidence chain" not in json.dumps(
        tool_call.result_summary, ensure_ascii=False
    )
    assert any(
        event.token_usage == {"input_tokens": 11, "output_tokens": 7} for event in node_events
    )
    plan_event = next(
        event
        for event in node_events
        if event.node_name == "constrained_plan" and event.event_type == "node_completed"
    )
    assert plan_event.output_summary["plan_steps"] == [
        {"position": 1, "description": "Read immutable research input."},
        {"position": 2, "description": "Write cited findings."},
    ]
    assert len(llm.calls) == 3


def test_research_workflow_repairs_one_invalid_citation_then_finalizes(tmp_path) -> None:
    repository, service, worker, project, task, package, bundle, llm = _research_stack(
        tmp_path, [{}, {}, {}, {}]
    )
    evidence_id = bundle.evidence[0].evidence_id
    llm.responses = [
        json.dumps(_analysis()),
        json.dumps(_plan()),
        json.dumps(_draft(bundle.claim.claim_id, evidence_id, valid=False)),
        json.dumps(_draft(bundle.claim.claim_id, evidence_id, valid=True)),
    ]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))

    assert worker.run_once()
    completed = service.get_run(run.id)
    output = service.repository.get_output(run.id)

    assert completed.status == "completed"
    assert completed.repair_count == 1
    assert output.validation["repair_applied"] is True
    assert len(llm.calls) == 4
    assert any(
        event.event_type == "repair_started" for event in service.repository.list_events(run.id)
    )
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1


def test_research_workflow_recovers_without_replaying_an_idempotent_snapshot_tool(tmp_path) -> None:
    _, service, worker, project, task, package, bundle, llm = _research_stack(
        tmp_path, [{}, {}, {}]
    )
    evidence_id = bundle.evidence[0].evidence_id
    llm.responses = [json.dumps(_analysis()), json.dumps(_plan()), RuntimeError("interrupted")]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))

    assert worker.run_once()
    assert service.get_run(run.id).status == "failed"
    assert len(service.repository.list_tool_calls(run.id)) == 1

    # The durable checkpoint resumes directly at the failed draft node.  It
    # must not repeat the completed Analysis or Plan LLM calls.
    llm.responses = [json.dumps(_draft(bundle.claim.claim_id, evidence_id, valid=True))]
    assert service.resume_run(run.id).status == "queued"
    assert worker.run_once()

    assert service.get_run(run.id).status == "completed"
    tool_calls = service.repository.list_tool_calls(run.id)
    assert len(tool_calls) == 1
    assert tool_calls[0].event_id
    assert len(llm.calls) == 4


def test_research_checkpoint_excludes_snapshot_content_and_draft_bodies(tmp_path) -> None:
    _, service, worker, project, task, package, bundle, llm = _research_stack(
        tmp_path, [{}, {}, {}]
    )
    evidence_id = bundle.evidence[0].evidence_id
    llm.responses = [
        json.dumps(_analysis()),
        json.dumps(_plan()),
        json.dumps(_draft(bundle.claim.claim_id, evidence_id, valid=True)),
    ]
    run = service.create_run(project.id, task.id, _research_request(package.snapshot_id))

    assert worker.run_once()
    checkpoint_path = Path(worker.agent_runtime.checkpoint_factory.path)
    with AgentCheckpointFactory(str(checkpoint_path)).open(
        namespace=RESEARCH_CHECKPOINT_NAMESPACE
    ) as checkpointer:
        saved = checkpointer.get_tuple(
            {
                "configurable": {
                    "thread_id": run.id,
                    "checkpoint_ns": RESEARCH_CHECKPOINT_NAMESPACE,
                }
            }
        )

    assert saved is not None
    values = saved.checkpoint["channel_values"]
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    with sqlite3.connect(checkpoint_path) as connection:
        namespaces = {
            row[0]
            for row in connection.execute("SELECT DISTINCT checkpoint_ns FROM checkpoints")
        }
    assert "context_package" not in values
    assert "tool_results" not in values
    assert "research_draft" not in values
    checkpoint_bytes = checkpoint_path.read_bytes()
    assert (
        "A governed ContextSnapshot retains a traceable claim-to-evidence chain."
        not in serialized
    )
    assert "Write a structured research draft" not in serialized
    assert (
        b"A governed ContextSnapshot retains a traceable claim-to-evidence chain."
        not in checkpoint_bytes
    )
    assert b"Write a structured research draft" not in checkpoint_bytes
    assert values["context_snapshot_id"] == package.snapshot_id
    assert values["context_sha256"] == package.package_sha256
    assert values["selected_claim_ids"] == [bundle.claim.claim_id]
    assert values["selected_evidence_ids"] == [evidence_id]
    assert namespaces == {RESEARCH_CHECKPOINT_NAMESPACE}


def test_research_workflow_stops_for_review_after_one_failed_repair(tmp_path) -> None:
    repository, service, worker, project, task, package, bundle, llm = _research_stack(
        tmp_path, [{}, {}, {}, {}]
    )
    evidence_id = bundle.evidence[0].evidence_id
    invalid = _draft(bundle.claim.claim_id, evidence_id, valid=False)
    llm.responses = [
        json.dumps(_analysis()),
        json.dumps(_plan()),
        json.dumps(invalid),
        json.dumps(invalid),
    ]
    run = service.create_run(
        project.id, task.id, _research_request(package.snapshot_id, propose=True)
    )

    assert worker.run_once()
    needs_review = service.get_run(run.id)

    assert needs_review.status == "needs_review"
    assert needs_review.repair_count == 1
    with pytest.raises(KeyError, match="no output"):
        service.repository.get_output(run.id)
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0

    # A worker crash after needs_review but before queue acknowledgement must
    # not replay the graph when the durable job lease is later reclaimed.
    repository.enqueue_job(
        kind="agent_run", resource_id=run.id, payload={}, force_requeue=True
    )
    assert worker.run_once()
    with repository._connect() as connection:
        job_status = connection.execute(
            "SELECT status FROM knowledge_jobs WHERE kind = 'agent_run' AND resource_id = ?",
            (run.id,),
        ).fetchone()["status"]
    assert job_status == "completed"
    assert len(llm.calls) == 4


def test_research_workflow_fails_before_finalization_when_token_budget_is_exhausted(
    tmp_path,
) -> None:
    repository, service, worker, project, task, package, _, llm = _research_stack(
        tmp_path, [{}, {}, {}]
    )
    llm.usage = {"input_tokens": 200, "output_tokens": 100}
    llm.responses = [
        json.dumps(_analysis()),
        json.dumps(_plan()),
        json.dumps(_draft("x", "y", valid=True)),
    ]
    run = service.create_run(
        project.id,
        task.id,
        AgentRunCreateRequest(
            workflow="research",
            context_snapshot_id=package.snapshot_id,
            max_steps=10,
            max_tool_calls=1,
            token_budget=256,
        ),
    )

    assert worker.run_once()
    failed = service.get_run(run.id)

    assert failed.status == "failed"
    assert "token budget" in (failed.error_message or "")
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_research_finalization_rolls_back_every_business_record_on_proposal_failure(
    tmp_path,
) -> None:
    repository, service, _, project, task, package, bundle, _ = _research_stack(tmp_path, [])
    run = service.create_run(
        project.id, task.id, _research_request(package.snapshot_id, propose=True)
    )
    service.mark_node(run.id, status="preparing", node_name="load_context", input_summary={})
    service.mark_node(run.id, status="running", node_name="research_draft", input_summary={})
    service.mark_node(
        run.id,
        status="validating",
        node_name="citation_validation",
        input_summary={},
    )
    evidence_id = bundle.evidence[0].evidence_id

    with pytest.raises(ValueError, match="unsupported MemoryProposal"):
        service.finalize_research_run(
            run.id,
            draft=_draft(bundle.claim.claim_id, evidence_id, valid=True),
            validation={"passed": True},
            memory_proposal_payload={"proposal_type": "unsupported"},
        )
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0] == 0
    assert service.get_run(run.id).status == "validating"


def test_research_run_requires_live_llm_and_snapshot_tools_fail_closed(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    project = memory.create_project(name="No LLM", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Research", goal="", priority="normal", metadata={}
    )
    service = AgentRunService(repository, llm=MockLLMClient())

    with pytest.raises(ResearchLLMRequiredError):
        service.create_run(project.id, task.id, AgentRunCreateRequest(workflow="research"))

    app = create_app(knowledge_repository=repository, agent_run_service=service)
    with TestClient(app) as client:
        response = client.post(
            f"/api/projects/{project.id}/workspace-tasks/{task.id}/agent-runs",
            json={"workflow": "research"},
        )
    assert response.status_code == 409

    package = ContextBuilderService(repository).build_context(
        ContextBuildRequest(task_id=task.id, project_id=project.id)
    )
    registry = build_research_tool_registry(package)
    with pytest.raises(ValueError, match="exact ContextSnapshot identity"):
        registry.execute(
            name="context.research_input",
            permission="context_read",
            arguments={"context_snapshot_id": "other", "context_sha256": "other"},
        )
