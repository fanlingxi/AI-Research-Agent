from __future__ import annotations

import sqlite3

import pytest

from app.context.models import ContextBuildRequest
from app.context.service import ContextBuilderService, ContextConflictError
from app.retrieval.contracts import CandidateReference
from tests.test_context_builder import _formal_entity
from tests.test_retrieval_audit import _context_setup


class _CallbackRetriever:
    def __init__(self, callback):
        self.callback = callback
        self.calls = 0

    def retrieve(self, query, **kwargs):
        self.calls += 1
        return self.callback(self.calls, query, kwargs)


def _counts(repository):
    with repository.database.connect() as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("context_snapshots", "context_snapshot_items"))


def _write(repository, sql):
    with repository.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(sql)


def test_retrieval_and_assembly_close_preparation_connection(tmp_path, monkeypatch):
    repository, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repository)
    connections = []
    snapshot = builder.memory_repository.snapshot_tx

    def capture(connection, project_id):
        connections.append(connection)
        return snapshot(connection, project_id)

    def assert_closed():
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[-1].execute("SELECT 1")

    def retrieve(*args):
        assert_closed()
        # An independent writer can commit while the external service is running.
        _write(repository, "UPDATE projects SET goal = 'Fresh context goal'")
        return []

    assemble = builder._assemble_package

    def check_assembly(*args, **kwargs):
        assert_closed()
        return assemble(*args, **kwargs)

    monkeypatch.setattr(builder.memory_repository, "snapshot_tx", capture)
    monkeypatch.setattr(builder, "_assemble_package", check_assembly)
    builder.vector_retriever = _CallbackRetriever(retrieve)
    package = builder.build(ContextBuildRequest(task_id=task.id, enable_vector_candidates=True))
    assert package.project.goal == "Fresh context goal"
    assert package.retrieval_audit.parameters["preparation_attempts"] == 2
    assert len(connections) == 4  # prepare/revalidate for each attempt
    assert _counts(repository)[0] == 1


@pytest.mark.parametrize("sql", [
    "UPDATE claims SET status = 'withdrawn'",
    "DELETE FROM collection_memberships",
    "DELETE FROM project_knowledge_scopes",
    "UPDATE chunks SET content = 'corrupt content without matching fingerprint'",
])
def test_retrieval_time_revocation_discards_candidates_and_reprepares(tmp_path, sql):
    repository, _, task, evidence = _context_setup(tmp_path)

    def retrieve(*args):
        _write(repository, sql)
        return [CandidateReference("vector", "chunk", evidence.chunk_id, 1.0)]

    retriever = _CallbackRetriever(retrieve)
    builder = ContextBuilderService(repository, vector_retriever=retriever)
    package = builder.build(ContextBuildRequest(task_id=task.id, enable_vector_candidates=True))
    assert retriever.calls == 1  # new empty scope/bundles must skip external IO
    assert package.knowledge.claim_bundles == []
    assert package.retrieval_audit.candidates == []
    assert package.retrieval_audit.parameters["preparation_attempts"] == 2
    assert package.retrieval_audit.parameters["previous_attempts"][0]["outcome"] == "inputs_changed"
    assert _counts(repository)[0] == 1


@pytest.mark.parametrize(("sql", "expected"), [
    ("UPDATE sources SET version = 'new-version'", "new-version"),
    ("UPDATE memory_decisions SET summary = 'New accepted constraint'", "New accepted constraint"),
    ("UPDATE artifacts SET reference = 'new-reference.md'", "new-reference.md"),
    ("UPDATE workspace_tasks SET goal = 'New task goal'", "New task goal"),
])
def test_content_changes_without_revision_bump_are_rechecked(tmp_path, sql, expected):
    repository, _, task, _ = _context_setup(tmp_path)
    queries = []

    def retrieve(attempt, query, kwargs):
        queries.append(query)
        if attempt == 1:
            _write(repository, sql)
        return []

    retriever = _CallbackRetriever(retrieve)
    builder = ContextBuilderService(repository, vector_retriever=retriever)
    package = builder.build(ContextBuildRequest(task_id=task.id, enable_vector_candidates=True))
    assert retriever.calls == 2
    assert expected in package.model_dump_json()
    assert package.retrieval_audit.parameters["preparation_attempts"] == 2
    if "workspace_tasks" in sql:
        assert "New task goal" in queries[1] and "New task goal" not in queries[0]
    assert builder.snapshot_repository.get(package.snapshot_id) == package


def test_scope_move_retrieves_again_and_rejects_old_scope_candidates(tmp_path):
    repository, _, task, old = _context_setup(tmp_path)
    new_scope = repository.create_collection("New context scope")
    new = _formal_entity(
        repository, collection_slug=new_scope.slug, entity_id="new-owner",
        title="Replacement Context Builder Method", suffix="New scope evidence.",
    )
    observed_scopes = []

    def retrieve(attempt, query, kwargs):
        observed_scopes.append(kwargs["collection_scopes"])
        if attempt == 1:
            project = repository.memory_repository.get_project(task.project_id)
            repository.memory_repository.replace_project_knowledge_scopes(
                project.id, [new_scope.slug], expected_project_revision=project.revision,
            )
        return [CandidateReference("vector", "chunk", old.chunk_id, 1.0)]

    retriever = _CallbackRetriever(retrieve)
    package = ContextBuilderService(repository, vector_retriever=retriever).build(
        ContextBuildRequest(task_id=task.id, enable_vector_candidates=True)
    )
    assert retriever.calls == 2 and observed_scopes[0] != observed_scopes[1]
    assert observed_scopes[1] == [new_scope.slug]
    assert package.knowledge.claim_bundles[0].chunks[0].chunk_id == new.chunk_id
    vector_audit = [item for item in package.retrieval_audit.candidates if item.channel == "vector"]
    assert len(vector_audit) == 1 and vector_audit[0].status == "rejected"


@pytest.mark.parametrize("sql", [
    "UPDATE workspace_tasks SET status = 'completed'",
    "UPDATE projects SET status = 'archived'",
])
def test_terminal_change_fails_without_retry_or_snapshot(tmp_path, sql):
    repository, _, task, _ = _context_setup(tmp_path)

    def retrieve(*args):
        _write(repository, sql)
        return []

    retriever = _CallbackRetriever(retrieve)
    builder = ContextBuilderService(repository, vector_retriever=retriever)
    with pytest.raises(ContextConflictError) as error:
        builder.build(ContextBuildRequest(task_id=task.id, enable_vector_candidates=True))
    assert error.value.preparation_attempts[0]["outcome"] == "ineligible"
    assert retriever.calls == 1
    assert _counts(repository) == (0, 0)


@pytest.mark.parametrize("preview", [False, True])
def test_continual_change_exhausts_two_preparations_without_snapshot(tmp_path, preview):
    repository, _, task, _ = _context_setup(tmp_path)

    def retrieve(*args):
        _write(repository, "UPDATE projects SET revision = revision + 1")
        raise TimeoutError("private transport details must not enter diagnostics")

    retriever = _CallbackRetriever(retrieve)
    builder = ContextBuilderService(repository, vector_retriever=retriever)
    build = builder.preview_context if preview else builder.build_context
    with pytest.raises(ContextConflictError, match="all 2 preparation attempts") as error:
        build(ContextBuildRequest(task_id=task.id, enable_vector_candidates=True))
    assert retriever.calls == 4  # two channel attempts in each of two preparations
    assert len(error.value.preparation_attempts) == 2
    for attempt in error.value.preparation_attempts:
        assert "vector_attempt_2_transient_failure" in attempt["notices"]
    assert "private transport" not in str(error.value.preparation_attempts)
    assert _counts(repository) == (0, 0)


def test_change_at_save_boundary_is_rechecked_and_guard_blocks_other_writers(tmp_path, monkeypatch):
    repository, _, task, _ = _context_setup(tmp_path)
    builder = ContextBuilderService(repository)
    save = builder.snapshot_repository.save
    calls = 0
    guarded = []

    def intercept(package, items, *, validate_state):
        nonlocal calls
        calls += 1
        if calls == 1:
            _write(repository, "UPDATE memory_decisions SET summary = 'Changed before save'")

        def guard(connection):
            validate_state(connection)
            assert connection.in_transaction
            # This independent connection cannot begin writing in the interval
            # between successful validation and the repository INSERTs.
            with sqlite3.connect(repository.path, timeout=0.01) as writer:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute("BEGIN IMMEDIATE")
            guarded.append(True)

        return save(package, items, validate_state=guard)

    monkeypatch.setattr(builder.snapshot_repository, "save", intercept)
    package = builder.build(ContextBuildRequest(task_id=task.id))
    assert calls == 2 and guarded == [True]
    assert package.memory.accepted_decisions[0].summary == "Changed before save"
    assert _counts(repository)[0] == 1
    _write(repository, "UPDATE projects SET revision = revision + 1")
    assert builder.snapshot_repository.get(package.snapshot_id) == package


def test_items_failure_rolls_back_snapshot_and_releases_write_lock(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    _write(repository, """CREATE TRIGGER fail_item BEFORE INSERT ON context_snapshot_items
        BEGIN SELECT RAISE(ABORT, 'simulated item failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated item failure"):
        ContextBuilderService(repository).build(ContextBuildRequest(task_id=task.id))
    assert _counts(repository) == (0, 0)
    _write(repository, "DROP TRIGGER fail_item")


def test_preview_rechecks_without_writes_and_adapter_cannot_mutate_request(tmp_path):
    repository, _, task, _ = _context_setup(tmp_path)
    request = ContextBuildRequest(task_id=task.id, enable_vector_candidates=True)

    def retrieve(attempt, query, kwargs):
        request.task_id = "mutated-by-caller"
        kwargs["collection_scopes"].clear()
        kwargs["allowed_document_ids"].clear()
        if attempt == 1:
            _write(repository, "UPDATE workspace_tasks SET goal = 'New preview goal'")
        return []

    retriever = _CallbackRetriever(retrieve)
    package = ContextBuilderService(repository, vector_retriever=retriever).preview_context(request)
    assert package.task.task_id == task.id
    assert package.task.goal == "New preview goal"
    assert len(package.knowledge.claim_bundles) == 1
    assert retriever.calls == 2
    assert _counts(repository) == (0, 0)


@pytest.mark.parametrize(("exception", "failures", "expected_calls", "unavailable"), [
    (TimeoutError, 1, 2, False),
    (ConnectionError, 10, 2, True),
    (ValueError, 10, 1, True),
])
def test_optional_retries_are_finite_and_preserve_failure_notices(
    tmp_path, exception, failures, expected_calls, unavailable,
):
    repository, _, task, evidence = _context_setup(tmp_path)

    def retrieve(attempt, query, kwargs):
        if attempt <= failures:
            raise exception("secret details")
        return [CandidateReference("vector", "chunk", evidence.chunk_id, 0.9)]

    retriever = _CallbackRetriever(retrieve)
    package = ContextBuilderService(repository, vector_retriever=retriever).build(
        ContextBuildRequest(task_id=task.id, enable_vector_candidates=True)
    )
    assert retriever.calls == expected_calls
    assert ("vector_unavailable" in package.diagnostics.notices) == unavailable
    assert "secret details" not in package.model_dump_json()
    assert len(package.knowledge.claim_bundles) == 1
    assert package.diagnostics.vector_candidates == (0 if unavailable else 1)
    assert package.retrieval_audit.parameters["preparation_attempts"] == 1
