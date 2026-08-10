"""First-party, governed Domain Plugin contracts.

Domain Plugins supply bounded workflow, tool, validation, and rendering
behaviour.  They never own a second Knowledge, Memory, Context, or Runtime
persistence layer.
"""

from app.domain_plugins.contracts import DomainPluginManifest, PluginPin
from app.domain_plugins.registry import DomainPluginRegistry, create_builtin_plugin_registry

__all__ = [
    "DomainPluginManifest",
    "DomainPluginRegistry",
    "PluginPin",
    "create_builtin_plugin_registry",
]
