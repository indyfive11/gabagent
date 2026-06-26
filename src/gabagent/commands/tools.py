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
        "launching an app). command_id is a STRING parameter — e.g. 'jellyfin.search', "
        "'tidal.recommendations'; NEVER call a capability name as a tool/function directly, always "
        "invoke it through run_command. Call list_capabilities first if you're unsure which ids exist. "
        "For media transport you can use the generic ids media.pause / media.play / media.stop / "
        "media.next / media.previous — they target whichever player is currently active, so you don't "
        "need to look up the provider-specific id first. Risky commands will ask the user to confirm."
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
            # The model sometimes wraps a top-level TOOL name (list_capabilities, rescan_capabilities,
            # even run_command itself) in run_command's command_id. That's never a catalog id, and a
            # difflib salvage would loop right back to the same name — "Unknown command: X. Try X."
            # Point it at the real tool instead of giving self-defeating advice.
            if registry.get_tool(command_id) is not None:
                return ToolResult(output="", error=(
                    f"'{command_id}' is its own tool, not a run_command capability — call {command_id} "
                    "directly as a tool. run_command is only for catalog command ids (e.g. 'media.pause')."))
            # Salvage a plausible-but-wrong id before giving up (helps weaker local/Aria turns).
            from gabagent.commands.resolve import (
                resolve_media_intent, closest_command_ids, best_match_ratio, AUTO_ROUTE_RATIO,
            )
            # 1) A generic media-transport guess → the actually-playing provider (tidal/jellyfin).
            media = await resolve_media_intent(ctx, command_id)
            if media is not None:
                real_id, extra = media
                resolved = catalog.get(real_id)
                if resolved is not None:
                    cmd, command_id, args = resolved, real_id, {**(args or {}), **extra}
            # 2) Otherwise a close catalog id → auto-route if very close, else suggest in one step.
            #    Strictness is config-tunable (stricter on a weak-STT `mobile` process); defaults to the
            #    historical 0.6/0.86 so an unconfigured install is unchanged.
            if cmd is None:
                cfg = getattr(ctx, "config", None)
                cutoff = getattr(cfg, "salvage_command_cutoff", 0.6)
                auto_ratio = getattr(cfg, "salvage_auto_route_ratio", AUTO_ROUTE_RATIO)
                near = closest_command_ids(command_id, catalog.ids(), cutoff=cutoff)
                if near and best_match_ratio(command_id, near[0]) >= auto_ratio:
                    cmd, command_id = catalog.get(near[0]), near[0]
                elif near:
                    return ToolResult(output="", error=(
                        f"Unknown command: {command_id}. Did you mean: {', '.join(near)}? "
                        "Pass one of those as command_id, or call list_capabilities."))
            if cmd is None:
                return ToolResult(output="", error=f"Unknown command: {command_id}. Try list_capabilities.")
        from gabagent.commands.backends import run_backend
        result = await run_backend(cmd, args or {}, ctx)
        if result.success:
            from gabagent.commands import usage
            usage.record(command_id)   # promote frequently-used commands into the hot set
        return result


@registry.register
class ListCapabilitiesTool(ToolBase):
    name = "list_capabilities"
    description = (
        "Look up device/media/desktop capabilities. With no arguments, returns a compact index of "
        "domains. Pass domain='media' to list that domain's commands with their exact ids and "
        "parameters, or query='play music' to search across all capabilities. Call this to get the "
        "exact command_id and args before run_command."
    )
    parameters = {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "List a domain's full commands, e.g. 'media'"},
            "query": {"type": "string", "description": "Keyword search across all capabilities, e.g. 'play music'"},
        },
        "required": [],
    }

    async def execute(self, ctx: AgentContext, domain: str | None = None,
                      query: str | None = None, **kwargs: Any) -> ToolResult:
        catalog = getattr(ctx, "command_catalog", None)
        if catalog is None or not catalog.all():
            return ToolResult(output="No device/media capabilities are available on this machine.")
        if query:
            matches = catalog.search(query)
            ids = [c.id for c in matches]
            return ToolResult(output=json.dumps(
                [s for s in catalog.summaries() if s["id"] in ids]))
        if domain:
            return ToolResult(output=json.dumps(catalog.summaries(domain)))
        # No filter → the index (domains + counts), not every command — keeps it tight.
        return ToolResult(output=json.dumps(catalog.index()))


@registry.register
class RescanCapabilitiesTool(ToolBase):
    name = "rescan_capabilities"
    description = "Re-detect available device/media capabilities (e.g. after starting a service)."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        from gabagent.commands.discovery import discover_capabilities
        ctx.command_catalog = await discover_capabilities(ctx)
        return ToolResult(output=f"Re-scanned: {len(ctx.command_catalog.all())} capabilities available.")
