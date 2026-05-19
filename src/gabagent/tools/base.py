from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, ClassVar, TYPE_CHECKING
from gabagent.api.models import ToolResult

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


class ToolBase(ABC):
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[dict] = {"type": "object", "properties": {}, "required": []}
    allows_parallel: ClassVar[bool] = True
    complexity: ClassVar[str] = "simple"

    @classmethod
    def schema(cls) -> dict:
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": cls.description,
                "parameters": cls.parameters,
            },
        }

    @abstractmethod
    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult: ...
