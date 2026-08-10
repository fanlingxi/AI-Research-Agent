"""Runtime entry point that binds business AgentRuns to LangGraph checkpoints."""

from __future__ import annotations

from typing import Any

from app.agent.checkpoint import AgentCheckpointFactory
from app.agent.errors import AgentRunCancelledError, AgentRunTerminalError
from app.agent.models import AgentRun
from app.agent.service import AgentRunService
from app.domain_plugins.registry import DomainPluginRegistry

FOUNDATION_CHECKPOINT_NAMESPACE = "agent_runtime_phase3a"
RESEARCH_CHECKPOINT_NAMESPACE = "agent_runtime_phase3b"


class AgentRuntime:
    """Execute one bounded workflow selected by the persisted AgentRun contract."""

    def __init__(
        self,
        service: AgentRunService,
        *,
        checkpoint_factory: AgentCheckpointFactory,
        workflow: Any | None = None,
        research_workflow: Any | None = None,
        plugin_registry: DomainPluginRegistry | None = None,
    ) -> None:
        self.service = service
        self.checkpoint_factory = checkpoint_factory
        # ``workflow`` and ``research_workflow`` are retained only as test and
        # integration injection compatibility. Normal dispatch is entirely
        # registry-driven and never imports a concrete domain workflow.
        self._workflow_override = workflow or research_workflow
        self.plugin_registry = plugin_registry or service.plugin_registry

    def execute_queued(self, run_id: str) -> AgentRun:
        run = self.service.get_run(run_id)
        # Queue records are scheduling facts only.  Reclaiming a lease after a
        # business terminal transition must complete the queue item without
        # attempting to execute the graph again.
        if run.status in {"cancelled", "completed", "needs_review", "stale_context"}:
            return run
        if run.status in {"preparing", "running", "validating"}:
            try:
                run = self.service.recover_interrupted_run(run_id)
            except AgentRunTerminalError:
                current = self.service.get_run(run_id)
                if current.status == "cancelled":
                    return current
                raise
        if run.status != "queued":
            raise AgentRunTerminalError("AgentRun must be queued before it can execute.")
        try:
            workflow, checkpoint_namespace = self._workflow_for(run)
            with self.checkpoint_factory.open(namespace=checkpoint_namespace) as checkpointer:
                graph = workflow.compile(checkpointer)
                configurable = {
                    "thread_id": run.id,
                }
                checkpoint = checkpointer.get_tuple({"configurable": configurable})
                self.service.mark_node(
                    run_id,
                    status="preparing",
                    node_name="load_context",
                    input_summary={
                        "thread_id": run_id,
                        "context_snapshot_id": run.context_snapshot_id,
                        "checkpoint_resume": checkpoint is not None,
                    },
                )
                if checkpoint is not None:
                    # Some resumed nodes (for example the foundation metadata
                    # tool) have no separate running transition.  Restore the
                    # business state before LangGraph schedules that node.
                    self.service.mark_node(
                        run_id,
                        status="running",
                        node_name="checkpoint_resume",
                        input_summary={"thread_id": run_id},
                    )
                graph_input = (
                    None
                    if checkpoint is not None
                    else {
                        "run_id": run.id,
                        "project_id": run.project_id,
                        "task_id": run.task_id,
                        "context_snapshot_id": run.context_snapshot_id,
                        "context_sha256": run.context_sha256,
                        "workflow_name": run.workflow_name,
                        "workflow_version": run.workflow_version,
                        "plugin_key": run.plugin.key,
                        "plugin_version": run.plugin.version,
                        "plugin_contract_version": run.plugin.contract_version,
                        "plugin_workflow_key": run.plugin.workflow_key,
                        "phase": "load_context",
                        "current_node": "load_context",
                        "steps_used": 0,
                        "tool_calls_used": 0,
                        "llm_calls_used": 0,
                        "repair_attempts": 0,
                    }
                )
                graph.invoke(
                    graph_input,
                    config={
                        # LangGraph also counts the initial input transition;
                        # the business limit counts executable workflow nodes.
                        "recursion_limit": run.max_steps + 1,
                        "configurable": configurable,
                    },
                    # A completed node must be durable before the next node
                    # starts.  This is what makes a worker retry resume from
                    # the next graph node rather than replaying prior LLM work.
                    durability="sync",
                )
        except AgentRunCancelledError:
            return self.service.get_run(run_id)
        except AgentRunTerminalError:
            # A concurrent cancellation is authoritative.  It must not turn
            # into a failed queue job after the graph has already stopped.
            current = self.service.get_run(run_id)
            if current.status == "cancelled":
                return current
            raise
        except Exception as exc:
            self.service.fail_run(
                run_id,
                error_code="runtime_execution_failed",
                error_message=str(exc),
            )
            raise
        return self.service.get_run(run_id)

    def _workflow_for(self, run: AgentRun) -> tuple[Any, str]:
        plugin = self.plugin_registry.resolve_pin(run.plugin)
        specification = plugin.workflow_spec(run.plugin.workflow_key)
        if self._workflow_override is not None:
            return self._workflow_override, specification.checkpoint_namespace
        runtime_port = self.service.runtime_port_for(plugin.manifest.runtime_port)
        return (
            plugin.build_workflow(runtime_port, run.plugin),
            specification.checkpoint_namespace,
        )
