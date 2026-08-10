"""Static, fail-closed registry for first-party Domain Plugins."""

from __future__ import annotations

from app.domain_plugins.contracts import DomainPlugin, DomainPluginManifest, PluginPin
from app.domain_plugins.errors import DomainPluginConflictError, DomainPluginNotFoundError

_PRIVILEGED_RUNTIME_PORT_OWNERS = {"research": "research"}


class DomainPluginRegistry:
    """Application-composed registry; no directory or database code loading."""

    def __init__(self, plugins: list[DomainPlugin] | None = None) -> None:
        self._plugins: dict[str, DomainPlugin] = {}
        for plugin in plugins or []:
            self.register(plugin)

    def register(self, plugin: DomainPlugin) -> None:
        key = plugin.manifest.key
        if key in self._plugins:
            raise ValueError(f"Domain Plugin {key} is already registered.")
        expected_owner = _PRIVILEGED_RUNTIME_PORT_OWNERS.get(plugin.manifest.runtime_port)
        if expected_owner is not None and key != expected_owner:
            raise ValueError(
                f"Domain Runtime port {plugin.manifest.runtime_port} is reserved for "
                f"the {expected_owner} plugin."
            )
        workflow_keys = [workflow.key for workflow in plugin.manifest.workflows]
        if len(workflow_keys) != len(set(workflow_keys)):
            raise ValueError(f"Domain Plugin {key} declares duplicate workflow keys.")
        self._plugins[key] = plugin

    def list_manifests(self) -> list[DomainPluginManifest]:
        return [self._plugins[key].manifest for key in sorted(self._plugins)]

    def get(self, key: str) -> DomainPlugin:
        plugin = self._plugins.get(key)
        if plugin is None:
            raise DomainPluginNotFoundError(f"Domain Plugin {key} is not installed.")
        return plugin

    def resolve_pin(self, pin: PluginPin) -> DomainPlugin:
        plugin = self.get(pin.key)
        manifest = plugin.manifest
        if (
            manifest.version != pin.version
            or manifest.contract_version != pin.contract_version
        ):
            raise DomainPluginConflictError(
                f"Domain Plugin {pin.key} version {pin.version} is unavailable."
            )
        plugin.workflow_spec(pin.workflow_key)
        return plugin

    def pin(self, key: str, workflow_key: str) -> PluginPin:
        plugin = self.get(key)
        plugin.workflow_spec(workflow_key)
        return PluginPin(
            key=plugin.manifest.key,
            version=plugin.manifest.version,
            contract_version=plugin.manifest.contract_version,
            workflow_key=workflow_key,
        )


def create_builtin_plugin_registry() -> DomainPluginRegistry:
    """Return the small, explicit set of reviewed first-party plugins."""

    # Import lazily so AgentRunService can compose its default registry without
    # an import cycle through the existing Research workflow module.
    from app.domain_plugins.game_modeling.plugin import GameModelingDomainPlugin
    from app.domain_plugins.research.plugin import ResearchDomainPlugin

    return DomainPluginRegistry([ResearchDomainPlugin(), GameModelingDomainPlugin()])
