"""Application service for explicit Project and WorkspaceTask plugin routing."""

from __future__ import annotations

from app.domain_plugins.errors import DomainPluginConflictError
from app.domain_plugins.models import (
    ProjectDomainPluginUpdateRequest,
    ProjectDomainPluginUpdateResult,
    WorkspaceTaskPluginBindRequest,
    WorkspaceTaskPluginBindResult,
)
from app.domain_plugins.registry import DomainPluginRegistry
from app.memory.repository import MemoryRepository


class DomainPluginService:
    """Validate static registrations before changing only routing facts.

    This service intentionally exposes no Plugin implementation objects to the
    API and never gives a Plugin a repository or connection.
    """

    def __init__(self, memory_repository: MemoryRepository, registry: DomainPluginRegistry) -> None:
        self.memory_repository = memory_repository
        self.registry = registry

    def list_available(self):
        return self.registry.list_manifests()

    def list_project_bindings(self, project_id: str):
        return self.memory_repository.list_project_domain_plugins(project_id)

    def set_project_binding(
        self,
        project_id: str,
        plugin_key: str,
        payload: ProjectDomainPluginUpdateRequest,
    ) -> ProjectDomainPluginUpdateResult:
        self.registry.get(plugin_key)
        project, binding = self.memory_repository.set_project_domain_plugin(
            project_id,
            plugin_key=plugin_key,
            status=payload.status,
            config=payload.config,
            expected_project_revision=payload.expected_project_revision,
        )
        return ProjectDomainPluginUpdateResult(project=project, binding=binding)

    def bind_workspace_task(
        self, task_id: str, payload: WorkspaceTaskPluginBindRequest
    ) -> WorkspaceTaskPluginBindResult:
        self.registry.get(payload.plugin_key)
        task = self.memory_repository.get_workspace_task(task_id)
        binding = self.memory_repository.get_project_domain_plugin(
            task.project_id, payload.plugin_key
        )
        if binding.status != "enabled":
            raise DomainPluginConflictError(
                f"Domain Plugin {payload.plugin_key} is not enabled for this Project."
            )
        updated = self.memory_repository.bind_workspace_task_domain_plugin(
            task_id,
            plugin_key=payload.plugin_key,
            expected_revision=payload.expected_revision,
        )
        return WorkspaceTaskPluginBindResult(task=updated)
