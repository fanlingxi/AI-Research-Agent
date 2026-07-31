from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.research import ToolCall, ToolResult


ToolHandler = Callable[..., ToolResult]


@dataclass
class ResearchTool:
    """A callable tool exposed to agents."""

    name: str
    description: str
    handler: ToolHandler

    def run(self, **kwargs: Any) -> ToolResult:
        return self.handler(**kwargs)


class ToolRegistry:
    """In-memory registry for Phase 1 tool calling."""

    def __init__(self) -> None:
        self._tools: dict[str, ResearchTool] = {}

    def register(self, tool: ResearchTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ResearchTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[ResearchTool]:
        return list(self._tools.values())

    def run(self, tool_call: ToolCall) -> ToolResult:
        tool = self.get(tool_call.tool_name)
        if tool is None:
            return ToolResult(
                tool_name=tool_call.tool_name,
                status="error",
                content=f"Tool '{tool_call.tool_name}' is not registered.",
            )

        try:
            return tool.run(**tool_call.arguments)
        except Exception as exc:  # pragma: no cover - defensive boundary for tools
            return ToolResult(
                tool_name=tool_call.tool_name,
                status="error",
                content=str(exc),
            )
