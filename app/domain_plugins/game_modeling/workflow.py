"""Finite snapshot-only LangGraph workflow for Game Modeling."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.domain_plugins.contracts import DomainFinalizationCommand
from app.domain_plugins.game_modeling.models import GameModelResult, ResolvedGameModel
from app.domain_plugins.game_modeling.state import GameModelAgentState
from app.domain_plugins.game_modeling.tools import (
    build_game_model_tool_registry,
    execute_game_formula,
    game_model_markdown,
    game_tool_audit_summary,
    resolve_game_model,
    validate_game_model_result,
)
from app.domain_plugins.ports import DomainRuntimePort


class GameModelingWorkflow:
    """Evaluate one published Formula under one published Patch version.

    Full ContextPackage data, the formula expression, and result body are
    intentionally local variables in each node.  Checkpoints retain only
    IDs, hashes, counters, and compact audit summaries needed for recovery.
    """

    def __init__(self, service: DomainRuntimePort) -> None:
        self.service = service

    def compile(self, checkpointer: Any):
        graph = StateGraph(GameModelAgentState)
        graph.add_node("load_context", self._load_context)
        graph.add_node("run_formula", self._run_formula)
        graph.add_node("finalize", self._finalize)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "run_formula")
        graph.add_edge("run_formula", "finalize")
        graph.add_edge("finalize", END)
        return graph.compile(checkpointer=checkpointer)

    def _load_context(self, state: GameModelAgentState) -> dict[str, Any]:
        self.service.mark_node(
            state["run_id"],
            status="running",
            node_name="load_context",
            input_summary={"context_snapshot_id": state["context_snapshot_id"]},
        )
        package = self.service.load_context_snapshot(state["run_id"])
        resolved = resolve_game_model(package)
        self.service.record_node_completed(
            state["run_id"],
            node_name="load_context",
            output_summary={
                "formula_claim_id": resolved.request.formula_claim_id,
                "patch_claim_id": resolved.request.patch_claim_id,
                "patch_version": resolved.request.patch_version,
            },
        )
        return self._progress(
            phase="run_formula",
            current_node="run_formula",
            steps_used=1,
            selected_claim_ids=[
                resolved.request.formula_claim_id,
                resolved.request.patch_claim_id,
            ],
            selected_evidence_ids=self._selected_evidence_ids(resolved),
            game_model_summary=self._model_summary(resolved),
        )

    def _run_formula(self, state: GameModelAgentState) -> dict[str, Any]:
        self._mark_running(state, "run_formula")
        package = self.service.load_context_snapshot(state["run_id"])
        arguments = {
            "context_snapshot_id": package.snapshot_id,
            "context_sha256": package.package_sha256,
        }
        registry = build_game_model_tool_registry(package)
        raw_result = registry.execute(
            name="game.run_formula",
            permission="deterministic_compute",
            arguments=arguments,
        )
        result = GameModelResult.model_validate(raw_result)
        call = self.service.record_tool_call(
            state["run_id"],
            sequence=1,
            tool_name="game.run_formula",
            permission="deterministic_compute",
            arguments=arguments,
            result_summary=game_tool_audit_summary(result),
            idempotency_key=f"{state['run_id']}:game.run_formula:1",
            node_name="run_formula",
        )
        self.service.record_node_completed(
            state["run_id"],
            node_name="run_formula",
            output_summary={
                "tool_call_id": call.id,
                "formula_claim_id": result.formula_claim_id,
                "patch_claim_id": result.patch_claim_id,
            },
        )
        return self._progress(
            phase="finalize",
            current_node="finalize",
            steps_used=2,
            tool_calls_used=self.service.get_run(state["run_id"]).tool_call_count,
            tool_call_ids=[call.id],
        )

    def _finalize(self, state: GameModelAgentState) -> dict[str, Any]:
        self.service.mark_node(
            state["run_id"],
            status="validating",
            node_name="finalize",
            input_summary={
                "tool_call_count": self.service.get_run(state["run_id"]).tool_call_count
            },
        )
        package = self.service.load_context_snapshot(state["run_id"])
        resolved = resolve_game_model(package)
        # Recomputing is cheap and pure; it gives finalization a fresh,
        # snapshot-validated result without ever restoring a large tool result
        # from the checkpoint.
        result = execute_game_formula(resolved)
        validation = validate_game_model_result(result, resolved)
        if not validation.passed:
            raise ValueError("Game model validation failed")
        run = self.service.get_run(state["run_id"])
        proposal_payload = None
        if run.options.create_memory_proposal:
            proposal_payload = {
                "proposal_type": "decision_create",
                "summary": (
                    f"Game model: {result.value:g} {result.unit} under patch "
                    f"{result.patch_version}"
                ),
                "rationale": (
                    "Deterministic result derived from published Formula and Patch evidence."
                ),
                "impact": "Review before relying on this modeling result in project decisions.",
                "metadata": {
                    "agent_run_id": run.id,
                    "context_snapshot_id": run.context_snapshot_id,
                    "formula_claim_id": result.formula_claim_id,
                    "patch_claim_id": result.patch_claim_id,
                    "patch_version": result.patch_version,
                },
            }
        finalization = self.service.finalize_run(
            state["run_id"],
            command=DomainFinalizationCommand(
                plugin=run.plugin,
                output_type="game_model_result",
                structured_output={
                    "kind": "game_model_result",
                    "game_model": result.model_dump(mode="json"),
                },
                rendered_text=game_model_markdown(result),
                validation=validation.model_dump(mode="json"),
                artifact_type="game_model_report",
                artifact_metadata={
                    "game_model": {
                        "formula_claim_id": result.formula_claim_id,
                        "patch_claim_id": result.patch_claim_id,
                        "patch_version": result.patch_version,
                        "formula_sha256": result.formula_sha256,
                        "formula_evidence_ids": result.formula_provenance.evidence_ids,
                        "patch_evidence_ids": result.patch_provenance.evidence_ids,
                        "formula_source_ids": result.formula_provenance.source_ids,
                        "patch_source_ids": result.patch_provenance.source_ids,
                        "formula_source_versions": result.formula_provenance.source_versions,
                        "patch_source_versions": result.patch_provenance.source_versions,
                    }
                },
                memory_proposal_payload=proposal_payload,
            ),
        )
        return self._progress(
            phase="completed",
            current_node="complete",
            steps_used=3,
            selected_claim_ids=[result.formula_claim_id, result.patch_claim_id],
            selected_evidence_ids=self._selected_evidence_ids(resolved),
            validation_summary={
                "passed": validation.passed,
                "cited_evidence_ids": validation.cited_evidence_ids,
                "patch_version": validation.patch_version,
            },
            output_id=finalization.output.id,
            artifact_id=finalization.artifact.id,
            memory_proposal_id=(
                finalization.memory_proposal.id if finalization.memory_proposal else None
            ),
        )

    @staticmethod
    def _progress(**updates: Any) -> dict[str, Any]:
        return updates

    def _mark_running(self, state: GameModelAgentState, node_name: str) -> None:
        self.service.mark_node(
            state["run_id"], status="running", node_name=node_name, input_summary={}
        )

    @staticmethod
    def _selected_evidence_ids(resolved: ResolvedGameModel) -> list[str]:
        return sorted(
            set(resolved.formula_provenance.evidence_ids)
            | set(resolved.patch_provenance.evidence_ids)
        )

    @staticmethod
    def _model_summary(resolved: ResolvedGameModel) -> dict[str, Any]:
        return {
            "formula_claim_id": resolved.request.formula_claim_id,
            "patch_claim_id": resolved.request.patch_claim_id,
            "patch_version": resolved.request.patch_version,
            "formula_sha256": resolved.formula_sha256,
            "parameter_names": sorted(resolved.request.parameters),
            "unit": resolved.formula.unit,
        }
