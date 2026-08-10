import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from app.knowledge.repository import KnowledgeRepository
from app.memory.repository import MemoryRepository
from app.memory.schemas import (
    ArtifactCreateProposalPayload,
    DecisionCreateProposalPayload,
    WorkspaceTaskUpdateProposalPayload,
)


def _memory(tmp_path) -> tuple[KnowledgeRepository, MemoryRepository]:
    knowledge = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    return knowledge, knowledge.memory_repository


def test_v0011_creates_memory_core_tables_and_replays_idempotently(tmp_path) -> None:
    knowledge, _ = _memory(tmp_path)

    assert knowledge.schema_version() == 15
    with knowledge._connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(memory_proposals)")
        }
    assert {
        "projects",
        "workspace_tasks",
        "memory_decisions",
        "artifacts",
        "project_knowledge_scopes",
        "memory_proposals",
        "context_snapshots",
        "context_snapshot_items",
        "agent_runs",
        "agent_run_events",
        "agent_tool_calls",
        "agent_run_outputs",
        "project_domain_plugins",
    }.issubset(tables)
    assert {"proposal_type", "payload_json", "committed_record_id", "revision"}.issubset(columns)

    reopened = KnowledgeRepository(knowledge.path)
    assert reopened.schema_version() == 15


def test_v11_fixture_upgrades_to_v12_without_changing_memory_business_data(tmp_path) -> None:
    knowledge, memory = _memory(tmp_path)
    collection = knowledge.create_collection("v11 fixture scope")
    project = memory.create_project(
        name="v11 Project", goal="Preserve Memory rows", domain="engineering", metadata={}
    )
    memory.replace_project_knowledge_scopes(
        project.id, [collection.slug], expected_project_revision=project.revision
    )
    task = memory.create_workspace_task(
        project_id=project.id,
        title="v11 task",
        goal="Verify an additive upgrade.",
        priority="high",
        metadata={},
    )
    memory.create_decision(
        project_id=project.id,
        task_id=task.id,
        summary="Keep the fixture stable",
        rationale="Migration must be additive",
        impact="Phase 2 snapshots begin empty",
        status="accepted",
        metadata={},
    )
    memory.create_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="note",
        reference="memory://v11-fixture",
        status="ready",
        metadata={},
    )
    memory.create_proposal(
        project_id=project.id,
        task_id=task.id,
        rationale="A real v11 proposal row.",
        payload=DecisionCreateProposalPayload(
            proposal_type="decision_create",
            summary="Proposed fixture decision",
            rationale="Test data",
            impact="No change until reviewed",
        ).model_dump(),
    )

    memory_tables = (
        "projects",
        "workspace_tasks",
        "memory_decisions",
        "artifacts",
        "project_knowledge_scopes",
        "memory_proposals",
    )
    with knowledge._connect() as connection:
        baseline = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in memory_tables
        }
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE agent_run_outputs")
        connection.execute("DROP TABLE agent_tool_calls")
        connection.execute("DROP TABLE agent_run_events")
        connection.execute("DROP TABLE agent_runs")
        connection.execute("DROP TABLE context_snapshot_items")
        connection.execute("DROP TABLE context_snapshots")
        connection.execute("DROP TABLE project_domain_plugins")
        connection.execute("DROP INDEX workspace_tasks_plugin_idx")
        connection.execute("ALTER TABLE workspace_tasks DROP COLUMN domain_plugin_key")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")
        connection.execute("DELETE FROM schema_migrations WHERE version = 14")
        connection.execute("DELETE FROM schema_migrations WHERE version = 13")
        connection.execute("DELETE FROM schema_migrations WHERE version = 12")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11

    upgraded = KnowledgeRepository(knowledge.path)
    assert upgraded.schema_version() == 15
    with upgraded._connect() as connection:
        after = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in memory_tables
        }
        assert after == baseline
        assert connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM context_snapshot_items").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reopened = KnowledgeRepository(knowledge.path)
    assert reopened.schema_version() == 15
    with reopened._connect() as connection:
        assert {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in memory_tables
        } == baseline


def test_project_tasks_and_knowledge_scopes_are_distinct(tmp_path) -> None:
    knowledge, memory = _memory(tmp_path)
    collection = knowledge.create_collection("Memory Scope")
    project = memory.create_project(
        name="Platform Foundation",
        goal="建立长期工作空间",
        domain="engineering",
        metadata={"owner": "local-user"},
    )
    task = memory.create_workspace_task(
        project_id=project.id,
        title="Define Memory contracts",
        goal="固定持久化边界",
        priority="high",
        metadata={},
    )
    scopes = memory.replace_project_knowledge_scopes(
        project.id, [collection.slug], expected_project_revision=project.revision
    )

    assert project.id.startswith("project-")
    assert task.id.startswith("workspace-task-")
    assert task.project_id == project.id
    assert [scope.collection_slug for scope in scopes] == [collection.slug]
    with knowledge._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM workspace_tasks").fetchone()[0] == 1

    other = memory.create_project(name="Other", goal="", domain="", metadata={})
    other_task = memory.create_workspace_task(
        project_id=other.id, title="Other task", goal="", priority="normal", metadata={}
    )
    with pytest.raises(ValueError, match="same Project"):
        memory.create_decision(
            project_id=project.id,
            task_id=other_task.id,
            summary="Invalid cross-project decision",
            rationale="",
            impact="",
            status="accepted",
            metadata={},
        )
    with pytest.raises(KeyError, match="Knowledge Collection"):
        memory.replace_project_knowledge_scopes(
            project.id,
            ["missing-collection"],
            expected_project_revision=memory.get_project(project.id).revision,
        )


def test_memory_statuses_and_revisions_use_optimistic_concurrency(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Review", goal="", domain="", metadata={})
    updated = memory.update_project(
        project.id, expected_revision=project.revision, goal="Revised project goal"
    )
    assert updated.revision == 2
    with pytest.raises(ValueError, match="revision conflict"):
        memory.update_project(project.id, expected_revision=1, domain="stale")

    paused = memory.transition_project(
        project.id, expected_revision=updated.revision, status="paused"
    )
    assert paused.status == "paused"
    archived = memory.transition_project(
        project.id, expected_revision=paused.revision, status="archived"
    )
    assert archived.status == "archived"

    active = memory.create_project(name="Task Project", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=active.id, title="Scoped task", goal="", priority="normal", metadata={}
    )
    ready = memory.transition_workspace_task(
        task.id, expected_revision=task.revision, status="ready"
    )
    assert ready.status == "ready"
    with pytest.raises(ValueError, match="invalid WorkspaceTask"):
        memory.transition_workspace_task(
            ready.id, expected_revision=ready.revision, status="completed"
        )


def test_public_updates_cannot_bypass_expected_revisions(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="CAS", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="CAS task", goal="", priority="normal", metadata={}
    )

    with pytest.raises(TypeError):
        memory.update_project(project.id, goal="missing revision")
    with pytest.raises(TypeError):
        memory.update_workspace_task(task.id, title="missing revision")
    with pytest.raises(ValueError, match="revision conflict"):
        memory.update_project(project.id, expected_revision=None, goal="invalid revision")
    with pytest.raises(ValueError, match="revision conflict"):
        memory.update_workspace_task(task.id, expected_revision=None, title="invalid revision")


def test_artifact_versions_only_store_references_and_do_not_write_files(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Artifacts", goal="", domain="", metadata={})
    file_reference = str(tmp_path / "never-written.md")
    artifact = memory.create_artifact(
        project_id=project.id,
        task_id=None,
        artifact_type="markdown",
        reference=file_reference,
        status="ready",
        metadata={"format": "md"},
    )
    version_two = memory.create_next_artifact_version(
        artifact.id,
        expected_revision=artifact.revision,
        reference=str(tmp_path / "also-never-written.md"),
        metadata=None,
    )

    assert artifact.version == 1
    assert version_two.version == 2
    assert version_two.status == "draft"
    assert version_two.supersedes_artifact_id == artifact.id
    assert memory.get_artifact(artifact.id).status == "superseded"
    assert not (tmp_path / "never-written.md").exists()
    assert not (tmp_path / "also-never-written.md").exists()


def test_artifact_versioning_uses_the_state_machine_for_draft_and_ready(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Version States", goal="", domain="", metadata={})

    for status in ("draft", "ready"):
        artifact = memory.create_artifact(
            project_id=project.id,
            task_id=None,
            artifact_type="note",
            reference=f"memory://{status}",
            status=status,
            metadata={},
        )
        successor = memory.create_next_artifact_version(
            artifact.id,
            expected_revision=artifact.revision,
            reference=f"memory://{status}-next",
            metadata=None,
        )

        superseded = memory.get_artifact(artifact.id)
        assert superseded.status == "superseded"
        assert successor.status == "draft"
        with pytest.raises(ValueError, match="only active Artifacts"):
            memory.create_next_artifact_version(
                superseded.id,
                expected_revision=superseded.revision,
                reference="memory://must-not-reactivate",
                metadata=None,
            )
        with pytest.raises(ValueError, match="invalid Artifact"):
            memory.transition_artifact(
                superseded.id, expected_revision=superseded.revision, status="ready"
            )


def test_direct_creation_rejects_terminal_memory_states(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Initial States", goal="", domain="", metadata={})

    with pytest.raises(ValueError, match="new Decisions"):
        memory.create_decision(
            project_id=project.id,
            task_id=None,
            summary="Terminal decision",
            rationale="",
            impact="",
            status="rejected",
            metadata={},
        )
    with pytest.raises(ValueError, match="new Artifacts"):
        memory.create_artifact(
            project_id=project.id,
            task_id=None,
            artifact_type="note",
            reference="memory://terminal",
            status="archived",
            metadata={},
        )


def test_scope_replacement_uses_project_revision_and_rolls_back_conflicts(tmp_path) -> None:
    knowledge, memory = _memory(tmp_path)
    first_collection = knowledge.create_collection("First scope")
    second_collection = knowledge.create_collection("Second scope")
    project = memory.create_project(name="Scoped CAS", goal="", domain="", metadata={})

    def replace(slug: str):
        try:
            return memory.replace_project_knowledge_scopes(
                project.id, [slug], expected_project_revision=project.revision
            )
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, [first_collection.slug, second_collection.slug]))

    successful = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, ValueError)]
    assert len(successful) == 1
    assert len(conflicts) == 1
    assert "revision conflict" in str(conflicts[0])
    assert memory.get_project(project.id).revision == project.revision + 1
    persisted = memory.list_project_knowledge_scopes(project.id)
    assert [scope.collection_slug for scope in persisted] == [successful[0][0].collection_slug]


def test_snapshot_reads_all_memory_from_one_read_only_transaction(tmp_path, monkeypatch) -> None:
    knowledge, memory = _memory(tmp_path)
    project = memory.create_project(name="Snapshot", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Open task", goal="", priority="normal", metadata={}
    )
    memory.create_decision(
        project_id=project.id,
        task_id=task.id,
        summary="Current decision",
        rationale="",
        impact="",
        status="accepted",
        metadata={},
    )
    memory.create_artifact(
        project_id=project.id,
        task_id=task.id,
        artifact_type="note",
        reference="memory://snapshot",
        status="ready",
        metadata={},
    )
    collection = knowledge.create_collection("Snapshot scope")
    memory.replace_project_knowledge_scopes(
        project.id, [collection.slug], expected_project_revision=project.revision
    )

    connection = memory._connect()
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    connects = 0

    def connect_once():
        nonlocal connects
        connects += 1
        return connection

    monkeypatch.setattr(memory, "_connect", connect_once)
    snapshot = memory.snapshot(project.id)
    connection.close()

    assert connects == 1
    assert any(statement == "PRAGMA query_only = ON" for statement in statements)
    assert any(statement == "BEGIN" for statement in statements)
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for statement in statements
    )
    assert snapshot.project.id == project.id
    assert [item.id for item in snapshot.workspace_tasks] == [task.id]
    assert len(snapshot.decisions) == 1
    assert len(snapshot.artifacts) == 1
    assert [scope.collection_slug for scope in snapshot.knowledge_scopes] == [collection.slug]


def test_memory_proposals_require_review_and_commit_once(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Proposal Project", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Decision task", goal="", priority="normal", metadata={}
    )
    proposal = memory.create_proposal(
        project_id=project.id,
        task_id=task.id,
        rationale="需要人工确认架构选择。",
        payload=DecisionCreateProposalPayload(
            proposal_type="decision_create",
            summary="Use SQLite as the Memory Core source of truth",
            rationale="Local-first architecture",
            impact="Future agents consume reviewed state",
        ).model_dump(),
    )

    with pytest.raises(ValueError, match="only approved"):
        memory.commit_proposal(proposal.id)
    approved = memory.review_proposal(
        proposal.id,
        expected_revision=proposal.revision,
        status="approved",
        review_note="confirmed",
    )
    first = memory.commit_proposal(approved.id)
    second = memory.commit_proposal(approved.id)

    assert first.proposal.status == "committed"
    assert first.record.id == second.record.id
    assert first.record.status == "accepted"
    assert len(memory.list_decisions(project.id)) == 1

    artifact_proposal = memory.create_proposal(
        project_id=project.id,
        task_id=task.id,
        rationale="记录已审核产物引用。",
        payload=ArtifactCreateProposalPayload(
            proposal_type="artifact_create",
            type="markdown",
            reference="memory://reviewed-artifact",
        ).model_dump(),
    )
    reviewed_artifact = memory.review_proposal(
        artifact_proposal.id,
        expected_revision=artifact_proposal.revision,
        status="approved",
        review_note=None,
    )
    committed_artifact = memory.commit_proposal(reviewed_artifact.id)
    assert committed_artifact.record.type == "markdown"
    assert len(memory.list_artifacts(project.id)) == 1


def test_workspace_task_update_proposal_validates_task_context_and_commits_atomically(
    tmp_path,
) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Update Proposal", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Original", goal="", priority="normal", metadata={}
    )
    with pytest.raises(ValueError, match="require task_id"):
        memory.create_proposal(
            project_id=project.id,
            task_id=None,
            rationale="",
            payload=WorkspaceTaskUpdateProposalPayload(
                proposal_type="workspace_task_update",
                expected_revision=task.revision,
                title="Missing target",
            ).model_dump(),
        )

    proposal = memory.create_proposal(
        project_id=project.id,
        task_id=task.id,
        rationale="改进任务描述。",
        payload=WorkspaceTaskUpdateProposalPayload(
            proposal_type="workspace_task_update",
            expected_revision=task.revision,
            title="Updated",
            status="ready",
        ).model_dump(),
    )
    approved = memory.review_proposal(
        proposal.id, expected_revision=proposal.revision, status="approved", review_note=None
    )
    result = memory.commit_proposal(approved.id)
    assert result.record.title == "Updated"
    assert result.record.status == "ready"
    assert result.record.revision == 2


def test_update_proposals_require_a_revision_and_roll_back_when_stale(tmp_path) -> None:
    _, memory = _memory(tmp_path)
    project = memory.create_project(name="Concurrent Proposal", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Original", goal="", priority="normal", metadata={}
    )

    with pytest.raises(ValidationError, match="expected_revision"):
        WorkspaceTaskUpdateProposalPayload(
            proposal_type="workspace_task_update", title="No revision"
        )

    proposal = memory.create_proposal(
        project_id=project.id,
        task_id=task.id,
        rationale="This proposal should become stale before it is committed.",
        payload=WorkspaceTaskUpdateProposalPayload(
            proposal_type="workspace_task_update",
            expected_revision=task.revision,
            title="Proposal value",
        ).model_dump(),
    )
    current = memory.update_workspace_task(
        task.id, expected_revision=task.revision, title="Newer direct value"
    )
    approved = memory.review_proposal(
        proposal.id, expected_revision=proposal.revision, status="approved", review_note=None
    )

    with pytest.raises(ValueError, match="revision conflict"):
        memory.commit_proposal(approved.id)

    assert memory.get_proposal(proposal.id).status == "approved"
    persisted = memory.get_workspace_task(task.id)
    assert persisted.title == current.title
    assert persisted.revision == current.revision


def test_memory_tables_preserve_foreign_key_integrity(tmp_path) -> None:
    knowledge, memory = _memory(tmp_path)
    project = memory.create_project(name="Integrity", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Integrity task", goal="", priority="normal", metadata={}
    )
    with knowledge._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspace_tasks (
                    id, project_id, title, goal, status, priority, metadata_json,
                    revision, created_at, updated_at
                ) VALUES (
                    'workspace-task-invalid', 'missing', 'Invalid', '', 'backlog', 'normal',
                    '{}', 1, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                )
                """
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert task.project_id == project.id
