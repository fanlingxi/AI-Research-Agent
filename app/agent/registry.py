"""Explicit tool registry and permission gate for Agent Runtime nodes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.agent.errors import AgentToolPermissionError
from app.agent.models import ToolPermission

ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    permission: ToolPermission
    handler: ToolHandler


class ToolRegistry:
    """Registry that rejects unknown tools and mismatched permissions."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name} is already registered.")
        self._tools[tool.name] = tool

    def execute(
        self, *, name: str, permission: ToolPermission, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise AgentToolPermissionError(f"Tool {name} is not registered.")
        if tool.permission != permission:
            raise AgentToolPermissionError(f"Tool {name} does not permit {permission}.")
        return tool.handler(dict(arguments))
