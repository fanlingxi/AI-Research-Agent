"""Minimal JSON-serializable LangGraph checkpoint state.

The checkpoint is operational recovery metadata only.  Research source content
always remains in the immutable ContextSnapshot and is reloaded, hash-checked,
and used ephemerally by the workflow node that needs it.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ResearchAgentState(TypedDict, total=False):
    run_id: str
    project_id: str
    task_id: str
    context_snapshot_id: str
    context_sha256: str

    # Persisted workflow progress.  These values are intentionally compact and
    # contain no ContextPackage, prompt, document content, evidence excerpts,
    # or complete tool responses.
    workflow_name: str
    workflow_version: str
    plugin_key: str
    plugin_version: str
    plugin_contract_version: str
    plugin_workflow_key: str
    phase: str
    current_node: str
    steps_used: int
    tool_calls_used: int
    llm_calls_used: int
    repair_attempts: int

    # Phase 3A foundation-only, identity-sized result.
    prepared: bool
    tool_result: dict[str, Any]

    # Small, structured Research progress.  Full snapshot data and draft
    # bodies are never checkpointed.
    task_analysis: dict[str, Any]
    research_plan: dict[str, Any]
    selected_claim_ids: list[str]
    selected_evidence_ids: list[str]
    tool_call_ids: list[str]
    validation_summary: dict[str, Any]
    needs_review: bool
    artifact_id: str
    memory_proposal_id: str | None
    output_id: str
    error_code: str
    safe_error_detail: str
