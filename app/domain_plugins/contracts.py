"""Small, framework-neutral contracts for governed first-party plugins."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class DomainWorkflowSpec(BaseModel):
    """One executable workflow offered by an installed Domain Plugin."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=2, max_length=160)
    version: str = Field(min_length=1, max_length=80)
    checkpoint_namespace: str = Field(min_length=2, max_length=200)
    min_steps: int = Field(ge=1, le=64)
    min_tool_calls: int = Field(ge=0, le=32)
    requires_live_llm: bool = False


class DomainPluginManifest(BaseModel):
    """Auditable, static metadata for a first-party plugin registration."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(min_length=1, max_length=80)
    contract_version: str = Field(min_length=1, max_length=40)
    display_name: str = Field(min_length=2, max_length=160)
    domain: str = Field(min_length=1, max_length=160)
    workflows: list[DomainWorkflowSpec] = Field(min_length=1, max_length=16)
    default_workflow_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    knowledge_node_types: list[str] = Field(default_factory=list, max_length=32)
    artifact_types: list[str] = Field(default_factory=list, max_length=32)
    allowed_permissions: list[str] = Field(default_factory=list, max_length=16)
    supports_memory_proposal: bool = False
    # Compatibility capability, not a plugin-controlled service locator. The
    # Runtime maps this closed value to a reviewed port implementation.
    runtime_port: Literal["generic", "research"] = "generic"


class PluginPin(BaseModel):
    """Exact plugin identity immutable for the life of one AgentRun."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(min_length=1, max_length=80)
    contract_version: str = Field(min_length=1, max_length=40)
    workflow_key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")


class DomainFinalizationCommand(BaseModel):
    """Plugin-described output, persisted atomically by the platform service.

    The command deliberately contains no persistence handles.  The Runtime
    validates the pin and owns output, Artifact, Proposal, and AgentRun writes
    in one transaction.
    """

    plugin: PluginPin
    output_type: str = Field(min_length=2, max_length=160)
    structured_output: dict[str, Any] = Field(default_factory=dict)
    rendered_text: str = Field(min_length=1, max_length=30000)
    validation: dict[str, Any] = Field(default_factory=dict)
    artifact_type: str = Field(min_length=2, max_length=120)
    artifact_metadata: dict[str, Any] = Field(default_factory=dict)
    memory_proposal_payload: dict[str, Any] | None = None


class DomainPlugin(Protocol):
    """A static plugin boundary; implementations receive only Runtime ports."""

    manifest: DomainPluginManifest

    def workflow_spec(self, workflow_key: str) -> DomainWorkflowSpec: ...

    def validate_run_request(
        self,
        workflow_key: str,
        *,
        max_steps: int,
        max_tool_calls: int,
        is_mock_llm: bool,
    ) -> None: ...

    def build_workflow(self, service: Any, pin: PluginPin) -> Any: ...
