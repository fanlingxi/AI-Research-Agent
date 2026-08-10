"""Safe failures for plugin selection and compatibility checks."""

from __future__ import annotations


class DomainPluginError(ValueError):
    """Base error for a governed Domain Plugin operation."""


class DomainPluginNotFoundError(KeyError):
    """The requested static plugin is not installed in this application."""


class DomainPluginConflictError(DomainPluginError):
    """The selected plugin is disabled or incompatible with the operation."""
