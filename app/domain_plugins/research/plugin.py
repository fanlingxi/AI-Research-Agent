"""Research plugin adapter around the frozen Phase 3 workflows."""

from __future__ import annotations

from typing import Any

from app.domain_plugins.contracts import DomainPluginManifest, DomainWorkflowSpec, PluginPin
from app.domain_plugins.errors import DomainPluginConflictError

RESEARCH_PLUGIN_KEY = "research"
RESEARCH_PLUGIN_VERSION = "phase5a-v1"
RESEARCH_PLUGIN_CONTRACT_VERSION = "1"


class ResearchDomainPlugin:
    """First-party adapter preserving the frozen Research workflow behaviour."""

    manifest = DomainPluginManifest(
        key=RESEARCH_PLUGIN_KEY,
        version=RESEARCH_PLUGIN_VERSION,
        contract_version=RESEARCH_PLUGIN_CONTRACT_VERSION,
        display_name="Research",
        domain="research",
        workflows=[
            DomainWorkflowSpec(
                key="research_v7", name="research_direct", version="structured-v6",
                checkpoint_namespace="agent_research_structured_v6", min_steps=9,
                min_tool_calls=1, requires_live_llm=True,
            ),
            DomainWorkflowSpec(
                key="research_v6", name="research_core_coverage", version="structured-v5",
                checkpoint_namespace="agent_research_structured_v5", min_steps=9,
                min_tool_calls=1, requires_live_llm=True,
            ),
            DomainWorkflowSpec(
                key="research_v5", name="research_coverage", version="structured-v4",
                checkpoint_namespace="agent_research_structured_v4", min_steps=9,
                min_tool_calls=1, requires_live_llm=True,
            ),
            DomainWorkflowSpec(
                key="research_v4", name="research_focused", version="structured-v3",
                checkpoint_namespace="agent_research_structured_v3", min_steps=9,
                min_tool_calls=1, requires_live_llm=True,
            ),
            DomainWorkflowSpec(
                key="research_v3", name="research_evidence_aligned", version="structured-v2",
                checkpoint_namespace="agent_research_structured_v2", min_steps=9,
                min_tool_calls=1, requires_live_llm=True,
            ),
            DomainWorkflowSpec(
                key='research_v2', name='research_structured', version='structured-v1',
                checkpoint_namespace='agent_research_structured_v1', min_steps=9,
                min_tool_calls=1, requires_live_llm=True,
            ),
            DomainWorkflowSpec(
                key="foundation",
                name="research_agent_foundation",
                version="phase3a-v1",
                checkpoint_namespace="agent_runtime_phase3a",
                min_steps=3,
                min_tool_calls=0,
            ),
            DomainWorkflowSpec(
                key="research",
                name="research_agent",
                version="phase3b-v1",
                checkpoint_namespace="agent_runtime_phase3b",
                min_steps=9,
                min_tool_calls=1,
                requires_live_llm=True,
            ),
        ],
        default_workflow_key="research",
        knowledge_node_types=["Paper", "Method", "Dataset", "Metric"],
        artifact_types=["research_report"],
        allowed_permissions=["context_read", "runtime_read"],
        supports_memory_proposal=True,
        runtime_port="research",
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
        workflow = self.workflow_spec(workflow_key)
        if max_steps < workflow.min_steps:
            raise ValueError(
                f"{workflow_key} workflow requires max_steps >= {workflow.min_steps}"
            )
        if max_tool_calls < workflow.min_tool_calls:
            raise ValueError(
                f"{workflow_key} workflow requires at least {workflow.min_tool_calls} tool call(s)"
            )
        if workflow.requires_live_llm and is_mock_llm:
            # Kept as a lazy import to preserve the lower-level plugin boundary.
            from app.agent.errors import ResearchLLMRequiredError

            raise ResearchLLMRequiredError(
                "Research Agent requires a configured live LLM; it will not fall back to mock."
            )

    def build_workflow(self, service: Any, pin: PluginPin) -> Any:
        workflow = self.workflow_spec(pin.workflow_key)
        if workflow.key == "research_v7":
            from app.agent.structured_research import DirectResearchWorkflow

            return DirectResearchWorkflow(service)
        if workflow.key == "research_v6":
            from app.agent.structured_research import CoreCoverageResearchWorkflow

            return CoreCoverageResearchWorkflow(service)
        if workflow.key == "research_v5":
            from app.agent.structured_research import CoverageResearchWorkflow

            return CoverageResearchWorkflow(service)
        if workflow.key == "research_v4":
            from app.agent.structured_research import FocusedResearchWorkflow

            return FocusedResearchWorkflow(service)
        if workflow.key == "research_v3":
            from app.agent.structured_research import EvidenceAlignedResearchWorkflow

            return EvidenceAlignedResearchWorkflow(service)
        if workflow.key == 'research_v2':
            from app.agent.structured_research import StructuredResearchWorkflow
            return StructuredResearchWorkflow(service)
        if workflow.key == "foundation":
            from app.agent.tools import build_foundation_tool_registry
            from app.agent.workflow import DeterministicFoundationWorkflow

            return DeterministicFoundationWorkflow(
                service, build_foundation_tool_registry()
            )
        if workflow.key == "research":
            from app.agent.research_workflow import ResearchWorkflow

            return ResearchWorkflow(service)
        raise DomainPluginConflictError(
            f"Domain Plugin {self.manifest.key} cannot build workflow {pin.workflow_key}."
        )
