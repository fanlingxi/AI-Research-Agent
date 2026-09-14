from __future__ import annotations

import json
import sqlite3

import pytest
from pydantic import ValidationError

from app.agent.models import AgentRunCreateRequest
from app.agent.service import AgentRunService
from app.context.models import ContextBuildRequest, canonical_package_sha256
from app.context.service import ContextBuilderService
from app.llms.provider import LangChainChatClient
from app.retrieval.contracts import CandidateReference
from app.retrieval.query_planning import QueryPlanner
from tests.test_context_builder import _formal_entity
from tests.test_retrieval_audit import _context_setup


class ScriptedLLM:
    provider_name = "test"
    model = "query-test"
    last_usage = {"input_tokens": 20, "output_tokens": 10}
    last_usage_complete = True

    def __init__(self, response, callback=None):
        self.response = response
        self.callback = callback
        self.calls = []

    def invoke(self, prompt, system_prompt=None):
        self.calls.append(json.loads(prompt))
        if self.callback:
            self.callback()
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_planner_preserves_original_year_dedupes_and_does_not_read_evidence():
    llm = ScriptedLLM(json.dumps({"queries": ["2024 RAG metrics", "2024 RAG metrics",
                                               "2024 evidence relevance"]}))
    plan = QueryPlanner(llm).plan("2024年的RAG评测")
    assert plan.queries == ["2024年的RAG评测", "2024 RAG metrics", "2024 evidence relevance"]
    assert plan.status == "planned" and plan.model_calls == 1
    assert plan.usage == llm.last_usage
    assert set(llm.calls[0]) == {"question", "max_additional_queries"}


@pytest.mark.parametrize("response", [
    "not-json", '{"queries": ["a", "b", "c", "d"]}',
    '{"queries": [42]}', '{"queries": [" "]}',
    '{"queries": ["ok"], "allowed_sources": ["outside"]}',
    json.dumps({"queries": ["x" * 513]}), TimeoutError("SECRET"),
])
def test_planner_invalid_output_or_failure_falls_back_without_retry(response):
    llm = ScriptedLLM(response)
    plan = QueryPlanner(llm).plan("original")
    assert plan.status == "fallback" and plan.queries == ["original"]
    assert len(llm.calls) == 1
    assert "SECRET" not in plan.model_dump_json()
    if isinstance(response, Exception):
        assert plan.usage is None  # An injected client's previous usage is not this failed call.


def test_unknown_usage_is_not_zero_and_oversized_question_never_calls_model():
    llm = ScriptedLLM('{"queries": ["another"]}')
    llm.last_usage_complete = False
    assert QueryPlanner(llm).plan("original").usage is None
    assert QueryPlanner(llm).plan("x" * 8001).model_calls == 0
    assert len(llm.calls) == 1


def test_disabled_and_failed_planner_preserve_legacy_selection(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    llm = ScriptedLLM("bad json")
    builder = ContextBuilderService(repository, query_planner=QueryPlanner(llm))
    old = builder.preview_context(ContextBuildRequest(task_id=task.id))
    assert not llm.calls
    fallback = builder.build_context(ContextBuildRequest(task_id=task.id,
                                                       query_planning="finite-v1"))
    assert [b.claim.claim_id for b in old.knowledge.claim_bundles] == [
        b.claim.claim_id for b in fallback.knowledge.claim_bundles]
    assert fallback.retrieval_audit.strategy_id == old.retrieval_audit.strategy_id
    assert fallback.retrieval_audit.parameters["query_planning"]["status"] == "fallback"
    assert builder.snapshot_repository.get(fallback.snapshot_id).package_sha256 == (
        canonical_package_sha256(fallback))


def test_planning_outside_transaction_revalidates_scope_and_never_repeats_model(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    connections = []
    builder = ContextBuilderService(repository)
    read = builder.memory_repository.snapshot_tx

    def capture(connection, project_id):
        connections.append(connection)
        return read(connection, project_id)

    builder.memory_repository.snapshot_tx = capture

    def revoke():
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[-1].execute("SELECT 1")
        with repository.database.connect() as db:
            db.execute("DELETE FROM collection_memberships")

    llm = ScriptedLLM('{"queries": ["evidence"]}', revoke)
    builder.query_planner = QueryPlanner(llm)
    package = builder.build_context(ContextBuildRequest(task_id=task.id,
                                                       query_planning="finite-v1"))
    assert len(llm.calls) == 1
    assert not package.knowledge.claim_bundles
    assert package.retrieval_audit.parameters["preparation_attempts"] == 2
    assert package.retrieval_audit.parameters["planning_attempt"]["model_calls"] == 1


def test_each_query_rejects_foreign_and_stale_candidates_and_preserves_budget(tmp_path):
    repository, _, task, evidence = _context_setup(tmp_path)
    foreign = repository.create_collection("Foreign")
    other = _formal_entity(repository, collection_slug=foreign.slug, entity_id="foreign",
                           title="Foreign", suffix="answer")
    baseline = ContextBuilderService(repository).preview_context(
        ContextBuildRequest(task_id=task.id))
    identity = baseline.retrieval_audit.selections[0].sources[0]

    class Retriever:
        calls = []

        def retrieve(self, query, **kwargs):
            self.calls.append((query, kwargs))
            return [CandidateReference("vector", "chunk", other.chunk_id, 1),
                    CandidateReference("vector", "chunk", evidence.chunk_id, 1,
                                       identity.model_copy(update={"source_version": "stale"})),
                    CandidateReference("vector", "chunk", evidence.chunk_id, .8, identity)]

    retriever = Retriever()
    llm = ScriptedLLM('{"queries": ["grounded package", "traceable context"]}')
    builder = ContextBuilderService(repository, vector_retriever=retriever,
                                    query_planner=QueryPlanner(llm))
    package = builder.build_context(ContextBuildRequest(
        task_id=task.id, query_planning="finite-v1",
        enable_vector_candidates=True, max_tokens=6000))
    assert len(retriever.calls) == 3
    assert all(other.paper_id not in kwargs["allowed_document_ids"]
               for _, kwargs in retriever.calls)
    rankings = package.retrieval_audit.parameters["query_rankings"]
    assert all([c["status"] for c in row["candidates"]] == ["rejected", "rejected", "verified"]
               for row in rankings)
    assert package.token_usage.used <= package.token_usage.budget
    assert all(chunk.chunk_id != other.chunk_id for bundle in package.knowledge.claim_bundles
               for chunk in bundle.chunks)


def test_task_ablation_does_not_call_model_and_existing_snapshot_cannot_replan(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    llm = ScriptedLLM(RuntimeError("must not call"))
    package = ContextBuilderService(repository, query_planner=QueryPlanner(llm)).build_context(
        ContextBuildRequest(task_id=task.id, query_planning="task-v1"))
    assert not llm.calls
    assert package.retrieval_audit.strategy_id == "project-context-query-task-v1"
    with pytest.raises(ValidationError, match="cannot be replanned"):
        AgentRunCreateRequest(context_snapshot_id=package.snapshot_id, query_planning="finite-v1")


def test_changed_task_discards_plan_and_preserves_attempt_audit(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)

    def change_goal():
        with repository.database.connect() as db:
            db.execute("UPDATE workspace_tasks SET goal = 'Changed Context Builder goal'")

    llm = ScriptedLLM('{"queries": ["previous question"]}', change_goal)
    package = ContextBuilderService(repository, query_planner=QueryPlanner(llm)).build_context(
        ContextBuildRequest(task_id=task.id, query_planning="finite-v1"))
    assert len(llm.calls) == 1
    assert package.task.goal == "Changed Context Builder goal"
    params = package.retrieval_audit.parameters
    assert params["query_planning"]["reason"] == "question_changed_after_plan"
    assert params["planning_attempt"]["model_calls"] == 1
    assert package.retrieval_audit.strategy_id == "project-context-legacy-v1"


def test_agent_creation_uses_requested_planning_and_audits_pre_run_usage(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    llm = ScriptedLLM('{"queries": ["traceable evidence"]}')
    service = AgentRunService(repository, llm=llm)
    run = service.create_run(task.project_id, task.id, AgentRunCreateRequest(
        query_planning="finite-v1", token_budget=16000))
    package = service.context_builder.snapshot_repository.get(run.context_snapshot_id)
    assert len(llm.calls) == 1
    assert package.retrieval_audit.strategy_id == "project-context-query-finite-v1"
    assert package.retrieval_audit.parameters["planning_usage_scope"] == (
        "pre_snapshot_not_agent_run")


def test_no_scope_skips_model_and_external_retrieval(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    with repository.database.connect() as db:
        db.execute("DELETE FROM project_knowledge_scopes")
    llm = ScriptedLLM(RuntimeError("must not call"))
    package = ContextBuilderService(repository, query_planner=QueryPlanner(llm)).build_context(
        ContextBuildRequest(task_id=task.id, query_planning="finite-v1",
                            enable_vector_candidates=True))
    assert not llm.calls and not package.knowledge.claim_bundles
    assert package.retrieval_audit.parameters["query_planning"]["reason"] == "no_verified_scope"


def test_live_evaluation_refuses_paid_entry_in_offline_tests(offline_trial_inputs):
    offline_trial_inputs("query_planning")
    from app.benchmarking.query_planning import run

    with pytest.raises(ValueError, match="disabled in offline tests"):
        run(execute=True)


def test_first_party_planning_caps_are_local_to_the_call(monkeypatch):
    client = LangChainChatClient("test", "model", "unused", "http://127.0.0.1:9",
                                max_tokens=32768, timeout=300, max_retries=4)
    called = []

    def invoke(self, prompt, system_prompt=None):
        called.append((self.max_tokens, self.timeout, self.max_retries))
        return '{"queries": ["search formulation"]}'

    monkeypatch.setattr(LangChainChatClient, "invoke", invoke)
    assert QueryPlanner(client).plan("question").status == "planned"
    assert called == [(1024, 60, 0)]
    assert (client.max_tokens, client.timeout, client.max_retries) == (32768, 300, 4)
