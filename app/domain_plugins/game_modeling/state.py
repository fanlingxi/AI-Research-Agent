"""Compact LangGraph state for the deterministic Game Modeling workflow."""

from __future__ import annotations

from typing import Any, TypedDict


class GameModelAgentState(TypedDict, total=False):
    run_id: str
    project_id: str
    task_id: str
    context_snapshot_id: str
    context_sha256: str
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
    selected_claim_ids: list[str]
    selected_evidence_ids: list[str]
    tool_call_ids: list[str]
    game_model_summary: dict[str, Any]
    validation_summary: dict[str, Any]
    artifact_id: str
    memory_proposal_id: str | None
    output_id: str
    error_code: str
    safe_error_detail: str
