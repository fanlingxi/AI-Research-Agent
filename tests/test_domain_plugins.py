from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.errors import AgentRunConflictError
from app.agent.models import AgentRunCreateRequest
from app.agent.service import AgentRunService
from app.api.main import create_app
from app.domain_plugins.contracts import (
    DomainFinalizationCommand,
    DomainPluginManifest,
    DomainWorkflowSpec,
    PluginPin,
)
from app.domain_plugins.errors import DomainPluginConflictError, DomainPluginNotFoundError
from app.domain_plugins.models import (
    ProjectDomainPluginUpdateRequest,
    WorkspaceTaskPluginBindRequest,
)
from app.domain_plugins.registry import DomainPluginRegistry, create_builtin_plugin_registry
from app.domain_plugins.service import DomainPluginService
from app.knowledge.repository import KnowledgeRepository
from app.llms.provider import MockLLMClient


class _FixturePlugin:
    manifest = DomainPluginManifest(
        key="fixture",
        version="fixture-v1",
        contract_version="1",
        display_name="Fixture Plugin",
        domain="fixture",
        workflows=[
            DomainWorkflowSpec(
                key="inspect",
                name="fixture_inspect",
                version="v1",
                checkpoint_namespace="fixture_inspect_v1",
                min_steps=3,
                min_tool_calls=0,
            )
        ],
        default_workflow_key="inspect",
        artifact_types=["fixture_output"],
    )

    def workflow_spec(self, workflow_key: str) -> DomainWorkflowSpec:
        if workflow_key != "inspect":
            raise DomainPluginConflictError("Fixture Plugin has no such workflow.")
        return self.manifest.workflows[0]

    def validate_run_request(
        self,
        workflow_key: str,
        *,
        max_steps: int,
        max_tool_calls: int,
        is_mock_llm: bool,
    ) -> None:
        self.workflow_spec(workflow_key)
        if max_steps < 3:
            raise ValueError("fixture workflow requires max_steps >= 3")

    def build_workflow(self, service, pin: PluginPin):
        raise AssertionError("Fixture workflow is only used to verify registration and pinning.")


def _registry_with_fixture() -> DomainPluginRegistry:
    registry = create_builtin_plugin_registry()
    registry.register(_FixturePlugin())
    return registry


def test_project_and_task_plugin_routing_is_explicit_cas_and_fail_closed(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    registry = _registry_with_fixture()
    domains = DomainPluginService(memory, registry)
    project = memory.create_project(name="Plugin Project", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Bound task", goal="", priority="normal", metadata={}
    )

    assert [
        (item.plugin_key, item.status) for item in domains.list_project_bindings(project.id)
    ] == [("research", "enabled")]
    with pytest.raises(DomainPluginNotFoundError):
        domains.set_project_binding(
            project.id,
            "missing",
            ProjectDomainPluginUpdateRequest(expected_project_revision=project.revision),
        )

    enabled = domains.set_project_binding(
        project.id,
        "fixture",
        ProjectDomainPluginUpdateRequest(
            expected_project_revision=project.revision,
            status="enabled",
            config={"allowed": True},
        ),
    )
    assert enabled.project.revision == project.revision + 1
    assert enabled.binding.status == "enabled"
    bound = domains.bind_workspace_task(
        task.id,
        WorkspaceTaskPluginBindRequest(
            expected_revision=task.revision, plugin_key="fixture"
        ),
    ).task
    assert bound.domain_plugin_key == "fixture"
    assert bound.revision == task.revision + 1

    with pytest.raises(ValueError, match="revision conflict"):
        domains.bind_workspace_task(
            task.id,
            WorkspaceTaskPluginBindRequest(expected_revision=task.revision, plugin_key="research"),
        )

    disabled = domains.set_project_binding(
        project.id,
        "fixture",
        ProjectDomainPluginUpdateRequest(
            expected_project_revision=enabled.project.revision,
            status="disabled",
        ),
    )
    assert disabled.binding.status == "disabled"
    with pytest.raises(DomainPluginConflictError, match="not enabled"):
        domains.bind_workspace_task(
            task.id,
            WorkspaceTaskPluginBindRequest(
                expected_revision=bound.revision, plugin_key="fixture"
            ),
        )


def test_privileged_research_runtime_port_cannot_be_self_declared() -> None:
    plugin = _FixturePlugin()
    plugin.manifest = plugin.manifest.model_copy(update={"runtime_port": "research"})
    with pytest.raises(ValueError, match="reserved"):
        DomainPluginRegistry().register(plugin)


def test_agent_run_pins_task_plugin_and_rejects_disabled_bindings(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    registry = _registry_with_fixture()
    domains = DomainPluginService(memory, registry)
    project = memory.create_project(name="Pinned", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Pinned task", goal="", priority="normal", metadata={}
    )
    enabled = domains.set_project_binding(
        project.id,
        "fixture",
        ProjectDomainPluginUpdateRequest(expected_project_revision=project.revision),
    )
    task = domains.bind_workspace_task(
        task.id,
        WorkspaceTaskPluginBindRequest(expected_revision=task.revision, plugin_key="fixture"),
    ).task
    agents = AgentRunService(
        repository, llm=MockLLMClient(), plugin_registry=registry
    )

    run = agents.create_run(
        project.id,
        task.id,
        AgentRunCreateRequest(workflow="inspect", max_steps=3, max_tool_calls=0),
    )
    assert run.plugin == PluginPin(
        key="fixture", version="fixture-v1", contract_version="1", workflow_key="inspect"
    )
    assert run.workflow_name == "fixture_inspect"
    assert run.workflow_version == "v1"

    # A terminal run no longer occupies the active-run uniqueness slot, so the
    # disabled binding is the authoritative reason the next create fails.
    agents.cancel_run(run.id)
    domains.set_project_binding(
        project.id,
        "fixture",
        ProjectDomainPluginUpdateRequest(
            expected_project_revision=enabled.project.revision,
            status="disabled",
        ),
    )
    with pytest.raises(DomainPluginConflictError, match="not enabled"):
        agents.create_run(
            project.id,
            task.id,
            AgentRunCreateRequest(workflow="inspect", max_steps=3, max_tool_calls=0),
        )


def test_generic_finalization_rejects_a_mismatched_plugin_pin_without_writes(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    project = memory.create_project(name="Finalize", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="Finalize task", goal="", priority="normal", metadata={}
    )
    agents = AgentRunService(repository, llm=MockLLMClient())
    run = agents.create_run(
        project.id,
        task.id,
        AgentRunCreateRequest(workflow="foundation", max_steps=3, max_tool_calls=0),
    )
    agents.mark_node(run.id, status="preparing", node_name="prepare", input_summary={})
    agents.mark_node(run.id, status="running", node_name="complete", input_summary={})
    agents.mark_node(run.id, status="validating", node_name="complete", input_summary={})

    with pytest.raises(AgentRunConflictError, match="PluginPin"):
        agents.finalize_run(
            run.id,
            command=DomainFinalizationCommand(
                plugin=PluginPin(
                    key="research",
                    version="wrong",
                    contract_version="1",
                    workflow_key="foundation",
                ),
                output_type="research_report",
                structured_output={},
                rendered_text="should not persist",
                artifact_type="research_report",
            ),
        )
    with repository._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_run_outputs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0

    port = agents.domain_runtime_port()
    assert not hasattr(port, "knowledge_repository")
    assert not hasattr(port, "context_builder")
    assert not hasattr(port, "repository")
    for research_only_method in (
        "llm",
        "complete_foundation_output",
        "begin_repair",
        "mark_needs_review",
    ):
        assert not hasattr(port, research_only_method)
    research_port = agents.research_runtime_port()
    assert hasattr(research_port, "complete_foundation_output")
    assert hasattr(research_port, "begin_repair")
    assert hasattr(research_port, "mark_needs_review")


def test_v14_to_current_plugin_migrations_preserve_existing_memory_rows(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    project = memory.create_project(name="v14", goal="preserve", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="legacy task", goal="", priority="normal", metadata={}
    )
    with repository._connect() as connection:
        baseline_project = dict(
            connection.execute("SELECT * FROM projects WHERE id = ?", (project.id,)).fetchone()
        )
        baseline_task = dict(
            connection.execute(
                """
                SELECT id, project_id, title, goal, status, priority, metadata_json,
                       revision, created_at, updated_at
                FROM workspace_tasks WHERE id = ?
                """,
                (task.id,),
            ).fetchone()
        )
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TABLE research_commands")
        connection.execute("DROP TABLE executor_heartbeats")
        connection.execute("DROP INDEX knowledge_jobs_claim_idx")
        connection.execute("ALTER TABLE knowledge_jobs DROP COLUMN priority")
        connection.execute("ALTER TABLE knowledge_jobs DROP COLUMN lease_owner")
        connection.execute("ALTER TABLE projection_outbox DROP COLUMN lease_owner")
        connection.execute("DROP INDEX agent_runs_plugin_idx")
        connection.execute("ALTER TABLE agent_runs DROP COLUMN plugin_workflow_key")
        connection.execute("ALTER TABLE agent_runs DROP COLUMN plugin_contract_version")
        connection.execute("ALTER TABLE agent_runs DROP COLUMN plugin_version")
        connection.execute("ALTER TABLE agent_runs DROP COLUMN plugin_key")
        connection.execute("DROP TABLE project_domain_plugins")
        connection.execute("DROP INDEX workspace_tasks_plugin_idx")
        connection.execute("ALTER TABLE workspace_tasks DROP COLUMN domain_plugin_key")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (15, 16, 17)")
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 14

    upgraded = KnowledgeRepository(repository.path)
    assert upgraded.schema_version() == 17
    with upgraded._connect() as connection:
        assert dict(
            connection.execute("SELECT * FROM projects WHERE id = ?", (project.id,)).fetchone()
        ) == baseline_project
        assert dict(
            connection.execute(
                """
                SELECT id, project_id, title, goal, status, priority, metadata_json,
                       revision, created_at, updated_at
                FROM workspace_tasks WHERE id = ?
                """,
                (task.id,),
            ).fetchone()
        ) == baseline_task
        assert connection.execute(
            """
            SELECT status FROM project_domain_plugins
            WHERE project_id = ? AND plugin_key = 'research'
            """,
            (project.id,),
        ).fetchone()["status"] == "enabled"
        assert connection.execute(
            "SELECT domain_plugin_key FROM workspace_tasks WHERE id = ?", (task.id,)
        ).fetchone()["domain_plugin_key"] == "research"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert KnowledgeRepository(repository.path).schema_version() == 17


def test_domain_plugin_api_is_versioned_and_never_exposes_implementation_objects(tmp_path) -> None:
    repository = KnowledgeRepository(str(tmp_path / "knowledge.db"))
    memory = repository.memory_repository
    project = memory.create_project(name="API", goal="", domain="", metadata={})
    task = memory.create_workspace_task(
        project_id=project.id, title="API task", goal="", priority="normal", metadata={}
    )
    agents = AgentRunService(repository, llm=MockLLMClient())
    app = create_app(knowledge_repository=repository, agent_run_service=agents)

    with TestClient(app) as client:
        manifests = client.get("/api/v1/domain-plugins")
        bindings = client.get(f"/api/v1/projects/{project.id}/domain-plugins")
        missing = client.put(
            f"/api/v1/projects/{project.id}/domain-plugins/missing",
            json={"expected_project_revision": project.revision},
        )
        disabled = client.put(
            f"/api/v1/projects/{project.id}/domain-plugins/research",
            json={"expected_project_revision": project.revision, "status": "disabled"},
        )
        task_bind = client.patch(
            f"/api/v1/workspace-tasks/{task.id}/domain-plugin",
            json={"expected_revision": task.revision, "plugin_key": "research"},
        )

    assert manifests.status_code == 200
    assert {item["key"] for item in manifests.json()} == {"game_modeling", "research"}
    game_manifest = next(item for item in manifests.json() if item["key"] == "game_modeling")
    assert game_manifest["domain"] == "game"
    assert game_manifest["knowledge_node_types"] == ["Formula", "Patch"]
    assert game_manifest["allowed_permissions"] == ["deterministic_compute"]
    assert game_manifest["runtime_port"] == "generic"
    assert "build_workflow" not in manifests.text
    assert bindings.json()[0]["plugin_key"] == "research"
    assert missing.status_code == 404
    assert disabled.status_code == 200
    assert task_bind.status_code == 409
