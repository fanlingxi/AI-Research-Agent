"""The bounded deterministic LangGraph workflow used by Phase 3A."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.registry import ToolRegistry
from app.agent.state import ResearchAgentState
from app.domain_plugins.research.ports import ResearchRuntimePort

FailureInjector = Callable[[str, str], None]


class DeterministicFoundationWorkflow:
    """Three finite nodes that exercise persistence without Research LLM logic."""

    def __init__(
        self,
        service: ResearchRuntimePort,
        registry: ToolRegistry,
        *,
        failure_injector: FailureInjector | None = None,
    ) -> None:
        self.service = service
        self.registry = registry
        self.failure_injector = failure_injector

    def compile(self, checkpointer: Any):
        graph = StateGraph(ResearchAgentState)
        graph.add_node("prepare", self._prepare)
        graph.add_node("inspect_context", self._inspect_context)
        graph.add_node("complete", self._complete)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "inspect_context")
        graph.add_edge("inspect_context", "complete")
        graph.add_edge("complete", END)
        return graph.compile(checkpointer=checkpointer)

    def _prepare(self, state: ResearchAgentState) -> dict[str, Any]:
        self.service.mark_node(
            state["run_id"],
            status="running",
            node_name="prepare",
            input_summary={"context_snapshot_id": state["context_snapshot_id"]},
        )
        return {"prepared": True}

    def _inspect_context(self, state: ResearchAgentState) -> dict[str, Any]:
        if self.failure_injector is not None:
            self.failure_injector("inspect_context", state["run_id"])
        arguments = {
            "context_snapshot_id": state["context_snapshot_id"],
            "context_sha256": state["context_sha256"],
        }
        run = self.service.get_run(state["run_id"])
        if run.max_tool_calls == 0:
            return {
                "tool_result": {
                    **arguments,
                    "skipped": "The AgentRun tool-call budget is zero.",
                }
            }
        result = self.registry.execute(
            name="context.snapshot_metadata", permission="context_read", arguments=arguments
        )
        self.service.record_tool_call(
            state["run_id"],
            sequence=1,
            tool_name="context.snapshot_metadata",
            permission="context_read",
            arguments=arguments,
            result_summary=result,
            idempotency_key=f"{state['run_id']}:context.snapshot_metadata:1",
        )
        return {"tool_result": result}

    def _complete(self, state: ResearchAgentState) -> dict[str, Any]:
        self.service.mark_node(
            state["run_id"],
            status="validating",
            node_name="complete",
            input_summary={"phase": "foundation"},
        )
        output = self.service.complete_foundation_output(
            state["run_id"], tool_result=state["tool_result"]
        )
        return {"output_id": output.id}
