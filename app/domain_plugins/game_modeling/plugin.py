"""First-party, deterministic Game Modeling Domain Plugin."""

from __future__ import annotations

from typing import Any

from app.domain_plugins.contracts import DomainPluginManifest, DomainWorkflowSpec, PluginPin
from app.domain_plugins.errors import DomainPluginConflictError

GAME_MODELING_PLUGIN_KEY = "game_modeling"
GAME_MODELING_PLUGIN_VERSION = "phase5b-v1"
GAME_MODELING_PLUGIN_CONTRACT_VERSION = "1"
GAME_MODELING_CHECKPOINT_NAMESPACE = "agent_runtime_phase5b_game_modeling"


class GameModelingDomainPlugin:
    """Static adapter for snapshot-only, evidence-grounded game calculations.

    This plugin intentionally supplies a workflow and a pure calculation tool;
    it owns neither a database nor an alternate Agent Runtime.  The platform
    still owns ContextSnapshots, AgentRuns, outputs, Artifacts, and proposals.
    """

    manifest = DomainPluginManifest(
        key=GAME_MODELING_PLUGIN_KEY,
        version=GAME_MODELING_PLUGIN_VERSION,
        contract_version=GAME_MODELING_PLUGIN_CONTRACT_VERSION,
        display_name="Game Modeling",
        domain="game",
        workflows=[
            DomainWorkflowSpec(
                key="model",
                name="game_modeling_agent",
                version="phase5b-v1",
                checkpoint_namespace=GAME_MODELING_CHECKPOINT_NAMESPACE,
                min_steps=3,
                min_tool_calls=1,
            )
        ],
        default_workflow_key="model",
        # This manifest states the facts the plugin actually consumes and can
        # govern today.  It does not advertise broader Game ontology support.
        knowledge_node_types=["Formula", "Patch"],
        artifact_types=["game_model_report"],
        allowed_permissions=["deterministic_compute"],
        supports_memory_proposal=True,
    )

    def workflow_spec(self, workflow_key: str) -> DomainWorkflowSpec:
        for workflow in self.manifest.workflows:
            if workflow.key == workflow_key:
                return workflow
        raise DomainPluginConflictError(
            f"Domain Plugin {self.manifest.key} does not support workflow {workflow_key}."
        )

    def validate_run_request(
        self,
        workflow_key: str,
        *,
        max_steps: int,
        max_tool_calls: int,
        is_mock_llm: bool,
    ) -> None:
        # ``is_mock_llm`` is accepted to meet the shared plugin contract. Game
        # Modeling makes no LLM call, so deterministic test execution is a
        # first-class supported mode rather than a fallback.
        del is_mock_llm
        workflow = self.workflow_spec(workflow_key)
        if max_steps < workflow.min_steps:
            raise ValueError(
                f"{workflow_key} workflow requires max_steps >= {workflow.min_steps}"
            )
        if max_tool_calls < workflow.min_tool_calls:
            raise ValueError(
                f"{workflow_key} workflow requires at least "
                f"{workflow.min_tool_calls} tool call(s)"
            )

    def build_workflow(self, service: Any, pin: PluginPin) -> Any:
        workflow = self.workflow_spec(pin.workflow_key)
        if workflow.key == "model":
            from app.domain_plugins.game_modeling.workflow import GameModelingWorkflow

            return GameModelingWorkflow(service)
        raise DomainPluginConflictError(
            f"Domain Plugin {self.manifest.key} cannot build workflow {pin.workflow_key}."
        )
