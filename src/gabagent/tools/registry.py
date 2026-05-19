from __future__ import annotations
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, type[ToolBase]] = {}

    def register(self, cls: type[ToolBase]) -> type[ToolBase]:
        self._tools[cls.name] = cls
        return cls

    def get_tool(self, name: str) -> type[ToolBase] | None:
        return self._tools.get(name)

    def get_schemas(self) -> list[dict]:
        return [cls.schema() for cls in self._tools.values()]

    async def dispatch(self, name: str, args: dict, ctx: AgentContext) -> ToolResult:
        cls = self._tools.get(name)
        if cls is None:
            return ToolResult(output="", error=f"Unknown tool: {name}")
        try:
            instance = cls()
            return await instance.execute(ctx, **args)
        except Exception as e:
            return ToolResult(output="", error=str(e))


registry = ToolRegistry()
