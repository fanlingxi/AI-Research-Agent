"""Fail closed before opening checkpoints or building an installed workflow."""

from app.agent.errors import WorkflowVersionMismatchError
from app.agent.models import AgentRun
from app.domain_plugins.contracts import DomainPlugin, DomainWorkflowSpec
from app.domain_plugins.errors import DomainPluginConflictError, DomainPluginNotFoundError
from app.domain_plugins.registry import DomainPluginRegistry


def resolve_compatible_workflow(
    run: AgentRun, registry: DomainPluginRegistry,
) -> tuple[DomainPlugin, DomainWorkflowSpec]:
    try:
        plugin = registry.resolve_pin(run.plugin)
        specification = plugin.workflow_spec(run.plugin.workflow_key)
    except (DomainPluginConflictError, DomainPluginNotFoundError) as exc:
        raise WorkflowVersionMismatchError() from exc
    if (
        run.plugin.workflow_key != specification.key
        or run.options.workflow != specification.key
        or run.workflow_name != specification.name
        or not run.workflow_version
        or run.workflow_version != specification.version
        # Runs created before the namespace was recorded rely on key + version,
        # which already determine the namespace in an installed specification.
        or (run.options.checkpoint_namespace is not None
            and run.options.checkpoint_namespace != specification.checkpoint_namespace)
    ):
        raise WorkflowVersionMismatchError()
    return plugin, specification
