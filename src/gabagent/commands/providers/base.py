from __future__ import annotations
from typing import Protocol, runtime_checkable, TYPE_CHECKING
from gabagent.commands.model import Command

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@runtime_checkable
class CapabilityProvider(Protocol):
    id: str

    async def detect(self, ctx: AgentContext) -> bool:
        """Is this capability available on this host (binary present, service reachable)?"""
        ...

    def commands(self, ctx: AgentContext) -> list[Command]:
        """The commands to publish into the catalog when detected."""
        ...
