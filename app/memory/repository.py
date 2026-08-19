from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.memory.models import (
    Artifact,
    MemoryDecision,
    MemoryProposal,
    MemoryProposalCommitResult,
    Project,
    ProjectDomainPlugin,
    ProjectKnowledgeScope,
    ProjectMemorySnapshot,
    WorkspaceTask,
)
from app.memory.schemas import (
    ArtifactCreateProposalPayload,
    DecisionCreateProposalPayload,
    ProjectUpdateProposalPayload,
    WorkspaceTaskCreateProposalPayload,
    WorkspaceTaskUpdateProposalPayload,
    validate_memory_proposal_payload,
)
from app.persistence.sqlite import SQLiteDatabase

_PROJECT_TRANSITIONS = {
    "active": {"paused", "completed", "archived"},
    "paused": {"active", "completed", "archived"},
    "completed": {"archived"},
    "archived": set(),
}
_TASK_TRANSITIONS = {
    "backlog": {"ready", "cancelled"},
    "ready": {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "cancelled"},
    "blocked": {"ready", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
_DECISION_TRANSITIONS = {
    "proposed": {"accepted", "rejected"},
    "accepted": {"superseded"},
    "superseded": set(),
    "rejected": set(),
}
_ARTIFACT_TRANSITIONS = {
    # Replacing a draft is a valid versioning operation: the old draft is no
    # longer the current draft once its successor has been created.
    "draft": {"ready", "superseded", "archived"},
    "ready": {"superseded", "archived"},
    "superseded": {"archived"},
    "archived": set(),
}


class MemoryRepository:
    """SQLite persistence boundary for long-lived project memory.

    Memory records are independent from Knowledge Core facts.  The only direct
    dependency is a project-owned scope link to existing Knowledge Collections.
    """

    def __init__(self, path: str, database: SQLiteDatabase | None = None) -> None:
        self.path = path
        self.database = database or SQLiteDatabase(path)

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect()

    # Projects -----------------------------------------------------------------

    def create_project(
        self, *, name: str, goal: str, domain: str, metadata: dict[str, Any]
    ) -> Project:
        now = _now()
        project_id = f"project-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO projects (
                    id, name, goal, domain, status, metadata_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, 1, ?, ?)
                """,
                (project_id, name, goal, domain, _dump(metadata), now, now),
            )
            # A first-party Research binding preserves the historical default
            # while making the Project's executable domain explicit.
            connection.execute(
                """
                INSERT INTO project_domain_plugins (
                    project_id, plugin_key, status, config_json, revision, created_at, updated_at
                ) VALUES (?, 'research', 'enabled', '{}', 1, ?, ?)
                """,
                (project_id, now, now),
            )
            return self._project_from_row(self._require_project_tx(connection, project_id))

    def get_project(self, project_id: str) -> Project:
        with self._connect() as connection:
            return self._project_from_row(self._require_project_tx(connection, project_id))

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        query = "SELECT * FROM projects"
        params: tuple[str, ...] = ()
        if not include_archived:
            query += " WHERE status != 'archived'"
        query += " ORDER BY updated_at DESC, name COLLATE NOCASE"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._project_from_row(row) for row in rows]

    def update_project(
        self,
        project_id: str,
        *,
        expected_revision: int,
        name: str | None = None,
        goal: str | None = None,
        domain: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Project:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._update_project_tx(
                connection,
                project_id,
                expected_revision=expected_revision,
                name=name,
                goal=goal,
                domain=domain,
                metadata=metadata,
            )

    def transition_project(
        self, project_id: str, *, expected_revision: int, status: str
    ) -> Project:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_project_tx(connection, project_id)
            self._require_revision(row, expected_revision)
            self._require_transition(_PROJECT_TRANSITIONS, str(row["status"]), status, "Project")
            now = _now()
            connection.execute(
                """
                UPDATE projects SET status = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (status, now, project_id),
            )
            return self._project_from_row(self._require_project_tx(connection, project_id))

    # Workspace tasks ----------------------------------------------------------

    def create_workspace_task(
        self,
        *,
        project_id: str,
        title: str,
        goal: str,
        priority: str,
        metadata: dict[str, Any],
    ) -> WorkspaceTask:
        now = _now()
        task_id = f"workspace-task-{uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._create_workspace_task_tx(
                connection,
                task_id=task_id,
                project_id=project_id,
                title=title,
                goal=goal,
                priority=priority,
                metadata=metadata,
                now=now,
            )

    def get_workspace_task(self, task_id: str) -> WorkspaceTask:
        with self._connect() as connection:
            return self._task_from_row(self._require_task_tx(connection, task_id))

    def read_workspace_task_tx(self, connection: sqlite3.Connection, task_id: str) -> WorkspaceTask:
        """Read one task using a caller-owned read transaction.

        Context Builder uses this narrowly scoped method together with
        :meth:`snapshot_tx` so Project Memory and Knowledge Core can be read
        from one SQLite snapshot. It intentionally has no update behaviour.
        """

        return self._task_from_row(self._require_task_tx(connection, task_id))

    def list_workspace_tasks(
        self, project_id: str, *, include_closed: bool = False
    ) -> list[WorkspaceTask]:
        with self._connect() as connection:
            self._require_project_tx(connection, project_id)
            query = "SELECT * FROM workspace_tasks WHERE project_id = ?"
            if not include_closed:
                query += " AND status NOT IN ('completed', 'cancelled')"
            query += (
                " ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'normal' THEN 2 ELSE 3 END, updated_at DESC"
            )
            rows = connection.execute(query, (project_id,)).fetchall()
        return [self._task_from_row(row) for row in rows]

    def update_workspace_task(
        self,
        task_id: str,
        *,
        expected_revision: int,
        title: str | None = None,
        goal: str | None = None,
        priority: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceTask:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._update_workspace_task_tx(
                connection,
                task_id,
                expected_revision=expected_revision,
                title=title,
                goal=goal,
                priority=priority,
                metadata=metadata,
            )

    def transition_workspace_task(
        self, task_id: str, *, expected_revision: int, status: str
    ) -> WorkspaceTask:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_task_tx(connection, task_id)
            self._require_revision(row, expected_revision)
            self._require_transition(_TASK_TRANSITIONS, str(row["status"]), status, "WorkspaceTask")
            connection.execute(
                """
                UPDATE workspace_tasks
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (status, _now(), task_id),
            )
            return self._task_from_row(self._require_task_tx(connection, task_id))

    # Domain Plugin bindings ---------------------------------------------------

    def list_project_domain_plugins(self, project_id: str) -> list[ProjectDomainPlugin]:
        with self._connect() as connection:
            self._require_project_tx(connection, project_id)
            rows = connection.execute(
                """
                SELECT * FROM project_domain_plugins
                WHERE project_id = ? ORDER BY plugin_key
                """,
                (project_id,),
            ).fetchall()
        return [self._project_domain_plugin_from_row(row) for row in rows]

    def get_project_domain_plugin(
        self, project_id: str, plugin_key: str
    ) -> ProjectDomainPlugin:
        with self._connect() as connection:
            return self._project_domain_plugin_from_row(
                self._require_project_domain_plugin_tx(connection, project_id, plugin_key)
            )

    def set_project_domain_plugin(
        self,
        project_id: str,
        *,
        plugin_key: str,
        status: str,
        config: dict[str, Any],
        expected_project_revision: int,
    ) -> tuple[Project, ProjectDomainPlugin]:
        """Update a Project binding and Project revision atomically."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = self._require_project_tx(connection, project_id)
            self._require_revision(project, expected_project_revision)
            now = _now()
            binding = connection.execute(
                """
                SELECT * FROM project_domain_plugins
                WHERE project_id = ? AND plugin_key = ?
                """,
                (project_id, plugin_key),
            ).fetchone()
            if binding is None:
                connection.execute(
                    """
                    INSERT INTO project_domain_plugins (
                        project_id, plugin_key, status, config_json, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (project_id, plugin_key, status, _dump(config), now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE project_domain_plugins
                    SET status = ?, config_json = ?, revision = revision + 1, updated_at = ?
                    WHERE project_id = ? AND plugin_key = ?
                    """,
                    (status, _dump(config), now, project_id, plugin_key),
                )
            connection.execute(
                """
                UPDATE projects SET revision = revision + 1, updated_at = ? WHERE id = ?
                """,
                (now, project_id),
            )
            return (
                self._project_from_row(self._require_project_tx(connection, project_id)),
                self._project_domain_plugin_from_row(
                    self._require_project_domain_plugin_tx(connection, project_id, plugin_key)
                ),
            )

    def bind_workspace_task_domain_plugin(
        self,
        task_id: str,
        *,
        plugin_key: str,
        expected_revision: int,
    ) -> WorkspaceTask:
        """Bind exactly one enabled Project plugin using WorkspaceTask CAS."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._require_task_tx(connection, task_id)
            self._require_revision(task, expected_revision)
            binding = self._require_project_domain_plugin_tx(
                connection, str(task["project_id"]), plugin_key
            )
            if str(binding["status"]) != "enabled":
                raise ValueError(f"Domain Plugin {plugin_key} is not enabled for this Project")
            connection.execute(
                """
                UPDATE workspace_tasks
                SET domain_plugin_key = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (plugin_key, _now(), task_id),
            )
            return self._task_from_row(self._require_task_tx(connection, task_id))

    def require_workspace_task_domain_plugin_tx(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        task_id: str,
        plugin_key: str,
    ) -> WorkspaceTask:
        """Re-check Task routing and Project enablement in a caller transaction."""

        task = self._require_project_task_tx(connection, project_id, task_id)
        if str(task["domain_plugin_key"]) != plugin_key:
            raise ValueError("WorkspaceTask Domain Plugin changed while creating AgentRun")
        binding = self._require_project_domain_plugin_tx(connection, project_id, plugin_key)
        if str(binding["status"]) != "enabled":
            raise ValueError(f"Domain Plugin {plugin_key} is not enabled for this Project")
        return self._task_from_row(task)

    # Decisions ----------------------------------------------------------------

    def create_decision(
        self,
        *,
        project_id: str,
        task_id: str | None,
        summary: str,
        rationale: str,
        impact: str,
        status: str,
        metadata: dict[str, Any],
    ) -> MemoryDecision:
        if status not in {"proposed", "accepted"}:
            raise ValueError("new Decisions must start as proposed or accepted")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._create_decision_tx(
                connection,
                decision_id=f"memory-decision-{uuid4().hex}",
                project_id=project_id,
                task_id=task_id,
                summary=summary,
                rationale=rationale,
                impact=impact,
                status=status,
                metadata=metadata,
                now=_now(),
            )

    def get_decision(self, decision_id: str) -> MemoryDecision:
        with self._connect() as connection:
            return self._decision_from_row(self._require_decision_tx(connection, decision_id))

    def list_decisions(
        self, project_id: str, *, include_inactive: bool = False
    ) -> list[MemoryDecision]:
        with self._connect() as connection:
            self._require_project_tx(connection, project_id)
            query = "SELECT * FROM memory_decisions WHERE project_id = ?"
            if not include_inactive:
                query += " AND status = 'accepted'"
            query += " ORDER BY updated_at DESC"
            rows = connection.execute(query, (project_id,)).fetchall()
        return [self._decision_from_row(row) for row in rows]

    def transition_decision(
        self, decision_id: str, *, expected_revision: int, status: str
    ) -> MemoryDecision:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_decision_tx(connection, decision_id)
            self._require_revision(row, expected_revision)
            self._require_transition(_DECISION_TRANSITIONS, str(row["status"]), status, "Decision")
            connection.execute(
                """
                UPDATE memory_decisions
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (status, _now(), decision_id),
            )
            return self._decision_from_row(self._require_decision_tx(connection, decision_id))

    # Artifacts ----------------------------------------------------------------

    def create_artifact(
        self,
        *,
        project_id: str,
        task_id: str | None,
        artifact_type: str,
        reference: str,
        status: str,
        metadata: dict[str, Any],
    ) -> Artifact:
        if status not in {"draft", "ready"}:
            raise ValueError("new Artifacts must start as draft or ready")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._create_artifact_tx(
                connection,
                artifact_id=f"artifact-{uuid4().hex}",
                project_id=project_id,
                task_id=task_id,
                artifact_type=artifact_type,
                reference=reference,
                version=1,
                status=status,
                supersedes_artifact_id=None,
                metadata=metadata,
                now=_now(),
            )

    def create_artifact_tx(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        task_id: str | None,
        artifact_type: str,
        reference: str,
        status: str,
        metadata: dict[str, Any],
    ) -> Artifact:
        """Create a first-version Artifact in a caller-owned transaction.

        This generic transaction entry point deliberately has no Agent-specific
        behaviour. It lets an orchestrating service make a ready Artifact and
        its related audit facts indivisible without giving the Agent SQL access.
        """

        return self._create_artifact_tx(
            connection,
            artifact_id=f"artifact-{uuid4().hex}",
            project_id=project_id,
            task_id=task_id,
            artifact_type=artifact_type,
            reference=reference,
            version=1,
            status=status,
            supersedes_artifact_id=None,
            metadata=metadata,
            now=_now(),
        )

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self._connect() as connection:
            return self._artifact_from_row(self._require_artifact_tx(connection, artifact_id))

    def list_artifacts(self, project_id: str, *, include_inactive: bool = False) -> list[Artifact]:
        with self._connect() as connection:
            self._require_project_tx(connection, project_id)
            query = "SELECT * FROM artifacts WHERE project_id = ?"
            if not include_inactive:
                query += " AND status IN ('draft', 'ready')"
            query += " ORDER BY updated_at DESC"
            rows = connection.execute(query, (project_id,)).fetchall()
        return [self._artifact_from_row(row) for row in rows]

    def create_next_artifact_version(
        self,
        artifact_id: str,
        *,
        expected_revision: int,
        reference: str,
        metadata: dict[str, Any] | None,
    ) -> Artifact:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = self._require_artifact_tx(connection, artifact_id)
            self._require_revision(prior, expected_revision)
            if prior["status"] not in {"draft", "ready"}:
                raise ValueError("only active Artifacts can receive a new version")
            self._require_transition(
                _ARTIFACT_TRANSITIONS, str(prior["status"]), "superseded", "Artifact"
            )
            now = _now()
            connection.execute(
                """
                UPDATE artifacts
                SET status = 'superseded', revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, artifact_id),
            )
            return self._create_artifact_tx(
                connection,
                artifact_id=f"artifact-{uuid4().hex}",
                project_id=str(prior["project_id"]),
                task_id=str(prior["task_id"]) if prior["task_id"] else None,
                artifact_type=str(prior["artifact_type"]),
                reference=reference,
                version=int(prior["version"]) + 1,
                status="draft",
                supersedes_artifact_id=artifact_id,
                metadata=metadata if metadata is not None else _json(prior["metadata_json"]),
                now=now,
            )

    def transition_artifact(
        self, artifact_id: str, *, expected_revision: int, status: str
    ) -> Artifact:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_artifact_tx(connection, artifact_id)
            self._require_revision(row, expected_revision)
            self._require_transition(_ARTIFACT_TRANSITIONS, str(row["status"]), status, "Artifact")
            connection.execute(
                """
                UPDATE artifacts
                SET status = ?, revision = revision + 1, updated_at = ? WHERE id = ?
                """,
                (status, _now(), artifact_id),
            )
            return self._artifact_from_row(self._require_artifact_tx(connection, artifact_id))

    # Project Knowledge scopes -------------------------------------------------

    def list_project_knowledge_scopes(self, project_id: str) -> list[ProjectKnowledgeScope]:
        with self._connect() as connection:
            self._require_project_tx(connection, project_id)
            rows = connection.execute(
                """
                SELECT project_id, collection_slug, created_at
                FROM project_knowledge_scopes WHERE project_id = ? ORDER BY collection_slug
                """,
                (project_id,),
            ).fetchall()
        return [ProjectKnowledgeScope.model_validate(dict(row)) for row in rows]

    def replace_project_knowledge_scopes(
        self,
        project_id: str,
        collection_slugs: list[str],
        *,
        expected_project_revision: int,
    ) -> list[ProjectKnowledgeScope]:
        selected = list(dict.fromkeys(collection_slugs))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = self._require_project_tx(connection, project_id)
            self._require_revision(project, expected_project_revision)
            self._require_collections_tx(connection, selected)
            connection.execute(
                "DELETE FROM project_knowledge_scopes WHERE project_id = ?", (project_id,)
            )
            now = _now()
            connection.executemany(
                """
                INSERT INTO project_knowledge_scopes (project_id, collection_slug, created_at)
                VALUES (?, ?, ?)
                """,
                [(project_id, slug, now) for slug in selected],
            )
            connection.execute(
                """
                UPDATE projects
                SET revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (now, project_id),
            )
            rows = connection.execute(
                """
                SELECT project_id, collection_slug, created_at
                FROM project_knowledge_scopes WHERE project_id = ? ORDER BY collection_slug
                """,
                (project_id,),
            ).fetchall()
        return [ProjectKnowledgeScope.model_validate(dict(row)) for row in rows]

    def snapshot(self, project_id: str) -> ProjectMemorySnapshot:
        """Read the current project memory view from one SQLite snapshot.

        Context construction is deliberately outside this persistence boundary;
        this method only supplies a coherent, read-only view of Memory Core.
        """

        with self._connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            return self.snapshot_tx(connection, project_id)

    def snapshot_tx(self, connection: sqlite3.Connection, project_id: str) -> ProjectMemorySnapshot:
        """Read Project Memory using a caller-owned transaction.

        The caller is responsible for starting a read-only transaction. Keeping
        this method transaction-scoped prevents a Context Package from mixing a
        Memory view from one connection with Knowledge rows from another.
        """

        project = self._project_from_row(self._require_project_tx(connection, project_id))
        task_rows = connection.execute(
            """
            SELECT * FROM workspace_tasks
            WHERE project_id = ? AND status NOT IN ('completed', 'cancelled')
            ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                WHEN 'normal' THEN 2 ELSE 3 END, updated_at DESC
            """,
            (project_id,),
        ).fetchall()
        decision_rows = connection.execute(
            """
            SELECT * FROM memory_decisions
            WHERE project_id = ? AND status = 'accepted'
            ORDER BY updated_at DESC
            """,
            (project_id,),
        ).fetchall()
        artifact_rows = connection.execute(
            """
            SELECT * FROM artifacts
            WHERE project_id = ? AND status IN ('draft', 'ready')
            ORDER BY updated_at DESC
            """,
            (project_id,),
        ).fetchall()
        scope_rows = connection.execute(
            """
            SELECT project_id, collection_slug, created_at
            FROM project_knowledge_scopes WHERE project_id = ? ORDER BY collection_slug
            """,
            (project_id,),
        ).fetchall()
        return ProjectMemorySnapshot(
            project=project,
            workspace_tasks=[self._task_from_row(row) for row in task_rows],
            decisions=[self._decision_from_row(row) for row in decision_rows],
            artifacts=[self._artifact_from_row(row) for row in artifact_rows],
            knowledge_scopes=[
                ProjectKnowledgeScope.model_validate(dict(row)) for row in scope_rows
            ],
        )

    # Proposals ----------------------------------------------------------------

    def create_proposal(
        self,
        *,
        project_id: str,
        task_id: str | None,
        payload: dict[str, Any],
        rationale: str,
    ) -> MemoryProposal:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.create_proposal_tx(
                connection,
                project_id=project_id,
                task_id=task_id,
                payload=payload,
                rationale=rationale,
            )

    def create_proposal_tx(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        task_id: str | None,
        payload: dict[str, Any],
        rationale: str,
    ) -> MemoryProposal:
        """Create a review-required proposal in a caller-owned transaction."""

        proposal_payload = validate_memory_proposal_payload(payload)
        if isinstance(proposal_payload, WorkspaceTaskUpdateProposalPayload) and task_id is None:
            raise ValueError("workspace_task_update proposals require task_id")
        proposal_id = f"memory-proposal-{uuid4().hex}"
        now = _now()
        self._require_project_tx(connection, project_id)
        if task_id is not None:
            self._require_project_task_tx(connection, project_id, task_id)
        connection.execute(
            """
            INSERT INTO memory_proposals (
                id, project_id, task_id, proposal_type, payload_json, rationale, status,
                revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', 1, ?, ?)
            """,
            (
                proposal_id,
                project_id,
                task_id,
                proposal_payload.proposal_type,
                _dump(proposal_payload.model_dump()),
                rationale,
                now,
                now,
            ),
        )
        return self._proposal_from_row(self._require_proposal_tx(connection, proposal_id))

    def get_proposal(self, proposal_id: str) -> MemoryProposal:
        with self._connect() as connection:
            return self._proposal_from_row(self._require_proposal_tx(connection, proposal_id))

    def list_proposals(
        self, project_id: str, *, include_closed: bool = False
    ) -> list[MemoryProposal]:
        with self._connect() as connection:
            self._require_project_tx(connection, project_id)
            query = "SELECT * FROM memory_proposals WHERE project_id = ?"
            if not include_closed:
                query += " AND status IN ('proposed', 'approved')"
            query += " ORDER BY updated_at DESC"
            rows = connection.execute(query, (project_id,)).fetchall()
        return [self._proposal_from_row(row) for row in rows]

    def review_proposal(
        self,
        proposal_id: str,
        *,
        expected_revision: int,
        status: str,
        review_note: str | None,
    ) -> MemoryProposal:
        if status not in {"approved", "rejected"}:
            raise ValueError("a MemoryProposal review must approve or reject")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._require_proposal_tx(connection, proposal_id)
            self._require_revision(row, expected_revision)
            if row["status"] != "proposed":
                raise ValueError("only proposed MemoryProposal records can be reviewed")
            now = _now()
            connection.execute(
                """
                UPDATE memory_proposals
                SET status = ?, review_note = ?, reviewed_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, review_note, now, now, proposal_id),
            )
            return self._proposal_from_row(self._require_proposal_tx(connection, proposal_id))

    def commit_proposal(self, proposal_id: str) -> MemoryProposalCommitResult:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal_row = self._require_proposal_tx(connection, proposal_id)
            if proposal_row["status"] == "committed":
                proposal = self._proposal_from_row(proposal_row)
                return MemoryProposalCommitResult(
                    proposal=proposal,
                    record=self._committed_record_tx(connection, proposal),
                )
            if proposal_row["status"] != "approved":
                raise ValueError("only approved MemoryProposal records can be committed")

            proposal = self._proposal_from_row(proposal_row)
            payload = validate_memory_proposal_payload(proposal.payload)
            record = self._commit_payload_tx(connection, proposal, payload)
            now = _now()
            connection.execute(
                """
                UPDATE memory_proposals
                SET status = 'committed', committed_record_type = ?, committed_record_id = ?,
                    committed_at = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (self._record_type(record), record.id, now, now, proposal_id),
            )
            committed = self._proposal_from_row(self._require_proposal_tx(connection, proposal_id))
            return MemoryProposalCommitResult(proposal=committed, record=record)

    # Transaction helpers ------------------------------------------------------

    def _update_project_tx(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        expected_revision: int,
        name: str | None,
        goal: str | None,
        domain: str | None,
        metadata: dict[str, Any] | None,
    ) -> Project:
        row = self._require_project_tx(connection, project_id)
        self._require_revision(row, expected_revision)
        connection.execute(
            """
            UPDATE projects
            SET name = ?, goal = ?, domain = ?, metadata_json = ?, revision = revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                name if name is not None else row["name"],
                goal if goal is not None else row["goal"],
                domain if domain is not None else row["domain"],
                _dump(metadata) if metadata is not None else row["metadata_json"],
                _now(),
                project_id,
            ),
        )
        return self._project_from_row(self._require_project_tx(connection, project_id))

    def _create_workspace_task_tx(
        self,
        connection: sqlite3.Connection,
        *,
        task_id: str,
        project_id: str,
        title: str,
        goal: str,
        priority: str,
        metadata: dict[str, Any],
        now: str,
    ) -> WorkspaceTask:
        self._require_project_tx(connection, project_id)
        connection.execute(
            """
            INSERT INTO workspace_tasks (
                id, project_id, title, goal, status, priority, domain_plugin_key, metadata_json,
                revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'backlog', ?, 'research', ?, 1, ?, ?)
            """,
            (task_id, project_id, title, goal, priority, _dump(metadata), now, now),
        )
        return self._task_from_row(self._require_task_tx(connection, task_id))

    def _update_workspace_task_tx(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        *,
        expected_revision: int,
        title: str | None,
        goal: str | None,
        priority: str | None,
        metadata: dict[str, Any] | None,
    ) -> WorkspaceTask:
        row = self._require_task_tx(connection, task_id)
        self._require_revision(row, expected_revision)
        connection.execute(
            """
            UPDATE workspace_tasks
            SET title = ?, goal = ?, priority = ?, metadata_json = ?, revision = revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                title if title is not None else row["title"],
                goal if goal is not None else row["goal"],
                priority if priority is not None else row["priority"],
                _dump(metadata) if metadata is not None else row["metadata_json"],
                _now(),
                task_id,
            ),
        )
        return self._task_from_row(self._require_task_tx(connection, task_id))

    def _create_decision_tx(
        self,
        connection: sqlite3.Connection,
        *,
        decision_id: str,
        project_id: str,
        task_id: str | None,
        summary: str,
        rationale: str,
        impact: str,
        status: str,
        metadata: dict[str, Any],
        now: str,
    ) -> MemoryDecision:
        if status not in {"proposed", "accepted"}:
            raise ValueError("new Decisions must start as proposed or accepted")
        self._require_project_tx(connection, project_id)
        if task_id is not None:
            self._require_project_task_tx(connection, project_id, task_id)
        connection.execute(
            """
            INSERT INTO memory_decisions (
                id, project_id, task_id, summary, rationale, impact, status, metadata_json,
                revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                decision_id,
                project_id,
                task_id,
                summary,
                rationale,
                impact,
                status,
                _dump(metadata),
                now,
                now,
            ),
        )
        return self._decision_from_row(self._require_decision_tx(connection, decision_id))

    def _create_artifact_tx(
        self,
        connection: sqlite3.Connection,
        *,
        artifact_id: str,
        project_id: str,
        task_id: str | None,
        artifact_type: str,
        reference: str,
        version: int,
        status: str,
        supersedes_artifact_id: str | None,
        metadata: dict[str, Any],
        now: str,
    ) -> Artifact:
        if status not in {"draft", "ready"}:
            raise ValueError("new Artifacts must start as draft or ready")
        self._require_project_tx(connection, project_id)
        if task_id is not None:
            self._require_project_task_tx(connection, project_id, task_id)
        if supersedes_artifact_id is not None:
            prior = self._require_artifact_tx(connection, supersedes_artifact_id)
            if prior["project_id"] != project_id:
                raise ValueError("Artifact versions cannot cross Project boundaries")
        connection.execute(
            """
            INSERT INTO artifacts (
                id, project_id, task_id, artifact_type, reference, version, status,
                supersedes_artifact_id, metadata_json, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                artifact_id,
                project_id,
                task_id,
                artifact_type,
                reference,
                version,
                status,
                supersedes_artifact_id,
                _dump(metadata),
                now,
                now,
            ),
        )
        return self._artifact_from_row(self._require_artifact_tx(connection, artifact_id))

    def _commit_payload_tx(
        self,
        connection: sqlite3.Connection,
        proposal: MemoryProposal,
        payload: Any,
    ) -> Project | WorkspaceTask | MemoryDecision | Artifact:
        now = _now()
        if isinstance(payload, DecisionCreateProposalPayload):
            return self._create_decision_tx(
                connection,
                decision_id=f"memory-decision-{uuid4().hex}",
                project_id=proposal.project_id,
                task_id=proposal.task_id,
                summary=payload.summary,
                rationale=payload.rationale,
                impact=payload.impact,
                status="accepted",
                metadata=payload.metadata,
                now=now,
            )
        if isinstance(payload, ArtifactCreateProposalPayload):
            return self._create_artifact_tx(
                connection,
                artifact_id=f"artifact-{uuid4().hex}",
                project_id=proposal.project_id,
                task_id=proposal.task_id,
                artifact_type=payload.type,
                reference=payload.reference,
                version=1,
                status="ready",
                supersedes_artifact_id=None,
                metadata=payload.metadata,
                now=now,
            )
        if isinstance(payload, WorkspaceTaskCreateProposalPayload):
            return self._create_workspace_task_tx(
                connection,
                task_id=f"workspace-task-{uuid4().hex}",
                project_id=proposal.project_id,
                title=payload.title,
                goal=payload.goal,
                priority=payload.priority,
                metadata=payload.metadata,
                now=now,
            )
        if isinstance(payload, ProjectUpdateProposalPayload):
            return self._update_project_tx(
                connection,
                proposal.project_id,
                expected_revision=payload.expected_revision,
                name=payload.name,
                goal=payload.goal,
                domain=payload.domain,
                metadata=payload.metadata,
            )
        if isinstance(payload, WorkspaceTaskUpdateProposalPayload):
            if proposal.task_id is None:
                raise ValueError("workspace_task_update proposals require task_id")
            task = self._require_project_task_tx(connection, proposal.project_id, proposal.task_id)
            self._require_revision(task, payload.expected_revision)
            if payload.status is not None:
                self._require_transition(
                    _TASK_TRANSITIONS, str(task["status"]), payload.status, "WorkspaceTask"
                )
            connection.execute(
                """
                UPDATE workspace_tasks
                SET title = ?, goal = ?, priority = ?, status = ?, metadata_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload.title if payload.title is not None else task["title"],
                    payload.goal if payload.goal is not None else task["goal"],
                    payload.priority if payload.priority is not None else task["priority"],
                    payload.status if payload.status is not None else task["status"],
                    _dump(payload.metadata)
                    if payload.metadata is not None
                    else task["metadata_json"],
                    _now(),
                    proposal.task_id,
                ),
            )
            return self._task_from_row(self._require_task_tx(connection, proposal.task_id))
        raise ValueError("unsupported MemoryProposal proposal_type")

    # Row conversion and validation -------------------------------------------

    @staticmethod
    def _require_revision(row: sqlite3.Row, expected_revision: int) -> None:
        if int(row["revision"]) != expected_revision:
            raise ValueError("revision conflict")

    @staticmethod
    def _require_transition(
        transitions: dict[str, set[str]], current: str, target: str, name: str
    ) -> None:
        if target not in transitions.get(current, set()):
            raise ValueError(f"invalid {name} status transition: {current} -> {target}")

    @staticmethod
    def _require_project_tx(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(f"Project {project_id} not found")
        return row

    @staticmethod
    def _require_task_tx(connection: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM workspace_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"WorkspaceTask {task_id} not found")
        return row

    @staticmethod
    def _require_project_domain_plugin_tx(
        connection: sqlite3.Connection, project_id: str, plugin_key: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM project_domain_plugins
            WHERE project_id = ? AND plugin_key = ?
            """,
            (project_id, plugin_key),
        ).fetchone()
        if row is None:
            raise KeyError(f"Domain Plugin {plugin_key} is not enabled for Project {project_id}")
        return row

    def _require_project_task_tx(
        self, connection: sqlite3.Connection, project_id: str, task_id: str
    ) -> sqlite3.Row:
        row = self._require_task_tx(connection, task_id)
        if row["project_id"] != project_id:
            raise ValueError("WorkspaceTask must belong to the same Project")
        return row

    @staticmethod
    def _require_decision_tx(connection: sqlite3.Connection, decision_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memory_decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Decision {decision_id} not found")
        return row

    @staticmethod
    def _require_artifact_tx(connection: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"Artifact {artifact_id} not found")
        return row

    @staticmethod
    def _require_proposal_tx(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memory_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"MemoryProposal {proposal_id} not found")
        return row

    @staticmethod
    def _require_collections_tx(connection: sqlite3.Connection, slugs: list[str]) -> None:
        if not slugs:
            return
        placeholders = ", ".join("?" for _ in slugs)
        rows = connection.execute(
            f"SELECT slug, is_system FROM knowledge_collections WHERE slug IN ({placeholders})",
            slugs,
        ).fetchall()
        found = {str(row["slug"]) for row in rows}
        missing = sorted(set(slugs) - found)
        if missing:
            raise KeyError(f"Knowledge Collection not found: {', '.join(missing)}")
        system_collections = sorted(str(row["slug"]) for row in rows if row["is_system"])
        if system_collections:
            raise ValueError(
                "System Knowledge Collections cannot be bound to a Project: "
                + ", ".join(system_collections)
            )

    def _committed_record_tx(
        self, connection: sqlite3.Connection, proposal: MemoryProposal
    ) -> Project | WorkspaceTask | MemoryDecision | Artifact:
        if not proposal.committed_record_type or not proposal.committed_record_id:
            raise ValueError("committed MemoryProposal has no target record")
        by_type = {
            "project": (self._require_project_tx, self._project_from_row),
            "workspace_task": (self._require_task_tx, self._task_from_row),
            "decision": (self._require_decision_tx, self._decision_from_row),
            "artifact": (self._require_artifact_tx, self._artifact_from_row),
        }
        getter = by_type.get(proposal.committed_record_type)
        if getter is None:
            raise ValueError("committed MemoryProposal target type is invalid")
        get_row, decode = getter
        return decode(get_row(connection, proposal.committed_record_id))

    @staticmethod
    def _record_type(record: Project | WorkspaceTask | MemoryDecision | Artifact) -> str:
        if isinstance(record, Project):
            return "project"
        if isinstance(record, WorkspaceTask):
            return "workspace_task"
        if isinstance(record, MemoryDecision):
            return "decision"
        return "artifact"

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            goal=row["goal"],
            domain=row["domain"],
            status=row["status"],
            metadata=_json(row["metadata_json"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> WorkspaceTask:
        return WorkspaceTask(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            goal=row["goal"],
            status=row["status"],
            priority=row["priority"],
            domain_plugin_key=row["domain_plugin_key"],
            metadata=_json(row["metadata_json"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _project_domain_plugin_from_row(row: sqlite3.Row) -> ProjectDomainPlugin:
        return ProjectDomainPlugin(
            project_id=row["project_id"],
            plugin_key=row["plugin_key"],
            status=row["status"],
            config=_json(row["config_json"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> MemoryDecision:
        return MemoryDecision(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            summary=row["summary"],
            rationale=row["rationale"],
            impact=row["impact"],
            status=row["status"],
            metadata=_json(row["metadata_json"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            type=row["artifact_type"],
            reference=row["reference"],
            version=row["version"],
            status=row["status"],
            supersedes_artifact_id=row["supersedes_artifact_id"],
            metadata=_json(row["metadata_json"]),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> MemoryProposal:
        return MemoryProposal(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            proposal_type=row["proposal_type"],
            payload=_json(row["payload_json"]),
            rationale=row["rationale"],
            status=row["status"],
            review_note=row["review_note"],
            committed_record_type=row["committed_record_type"],
            committed_record_id=row["committed_record_id"],
            revision=row["revision"],
            created_at=row["created_at"],
            reviewed_at=row["reviewed_at"],
            committed_at=row["committed_at"],
            updated_at=row["updated_at"],
        )


def _dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json(value: str) -> dict[str, Any]:
    return json.loads(value or "{}")


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
