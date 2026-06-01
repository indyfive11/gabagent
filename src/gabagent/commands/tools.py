"""The model-facing surface of the command framework: three tools cover everything.

`run_command` runs any catalog command (tier resolved from the catalog at the gate);
`list_capabilities` enumerates what's available; `rescan_capabilities` re-probes. New
capabilities/skills add catalog entries with ZERO new tool schemas.
"""
from __future__ import annotations
import json
from typing import Any, TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class RunCommandTool(ToolBase):
    name = "run_command"
    description = (
        "Run a detected device/media capability by its command_id (e.g. media playback, "
        "launching an app). Call list_capabilities first if you're unsure which ids exist. "
        "Risky commands will ask the user to confirm."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command_id": {"type": "string", "description": "Catalog command id, e.g. 'media.playpause'"},
            "args": {"type": "object", "description": "Parameters for the command (see list_capabilities)"},
        },
        "required": ["command_id"],
    }
    allows_parallel = False

    async def execute(self, ctx: AgentContext, command_id: str = "", args: dict | None = None, **kwargs: Any) -> ToolResult:
        catalog = getattr(ctx, "command_catalog", None)
        if catalog is None:
            return ToolResult(output="", error="Capabilities haven't been discovered on this machine.")
        cmd = catalog.get(command_id)
        if cmd is None:
            return ToolResult(output="", error=f"Unknown command: {command_id}. Try list_capabilities.")
        from gabagent.commands.backends import run_backend
        return await run_backend(cmd, args or {}, ctx)


@registry.register
class ListCapabilitiesTool(ToolBase):
    name = "list_capabilities"
    description = "List the device/media capabilities available on this machine (optionally filter by domain)."
    parameters = {
        "type": "object",
        "properties": {"domain": {"type": "string", "description": "Optional domain filter, e.g. 'media'"}},
        "required": [],
    }

    async def execute(self, ctx: AgentContext, domain: str | None = None, **kwargs: Any) -> ToolResult:
        catalog = getattr(ctx, "command_catalog", None)
        if catalog is None or not catalog.all():
            return ToolResult(output="No device/media capabilities are available on this machine.")
        return ToolResult(output=json.dumps(catalog.summaries(domain)))


@registry.register
class RescanCapabilitiesTool(ToolBase):
    name = "rescan_capabilities"
    description = "Re-detect available device/media capabilities (e.g. after starting a service)."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        from gabagent.commands.discovery import discover_capabilities
        ctx.command_catalog = await discover_capabilities(ctx)
        return ToolResult(output=f"Re-scanned: {len(ctx.command_catalog.all())} capabilities available.")
