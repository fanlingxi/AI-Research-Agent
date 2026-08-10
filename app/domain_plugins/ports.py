"""The deliberately small Runtime surface supplied to Domain Plugins."""

from __future__ import annotations

from typing import Any

from app.domain_plugins.contracts import DomainFinalizationCommand


class DomainRuntimePort:
    """Delegate only bounded Runtime operations to a plugin workflow.

    No repository, database connection, filesystem, projection, or queue is
    exposed here. The wrapped service remains private implementation detail of
    application composition rather than part of the plugin contract.
    """

    def __init__(self, service: Any) -> None:
        self.__service = service

    def get_run(self, run_id: str):
        return self.__service.get_run(run_id)

    def mark_node(self, run_id: str, **kwargs: Any):
        return self.__service.mark_node(run_id, **kwargs)

    def record_node_completed(self, run_id: str, **kwargs: Any) -> None:
        self.__service.record_node_completed(run_id, **kwargs)

    def record_tool_call(self, run_id: str, **kwargs: Any):
        return self.__service.record_tool_call(run_id, **kwargs)

    def load_context_snapshot(self, run_id: str):
        return self.__service.load_context_snapshot(run_id)

    def finalize_run(self, run_id: str, *, command: DomainFinalizationCommand):
        return self.__service.finalize_run(run_id, command=command)
