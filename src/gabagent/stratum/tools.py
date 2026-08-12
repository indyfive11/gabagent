"""Agent-callable Stratum tools. New names only — never shadow ``memory_write`` (the registry is
last-writer-wins by name). Registered only when ``config.stratum.enabled`` (gated in
``cli._register_tools``), so a disabled install never sees them.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry
from gabagent.stratum import active

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_DISABLED = "Stratum is disabled (set config.stratum.enabled = true)."


@registry.register
class CurrentFocusUpdateTool(ToolBase):
    name = "current_focus_update"
    description = (
        "Replace the '## Current Focus' window at the top of project memory. It is a WINDOW (a "
        "snapshot of now: Doing / Blocked / Done(≤3, dated) / Next), not a log — replace, never append."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The Current Focus body (below the heading)."}
        },
        "required": ["content"],
    }

    async def execute(self, ctx: AgentContext, content: str, **kwargs: Any) -> ToolResult:
        if not active(ctx):
            return ToolResult(output=_DISABLED)
        from gabagent.session.memory import MemoryManager
        from gabagent.stratum.current_focus import upsert_block
        mgr = MemoryManager(ctx.cwd)
        mgr.rewrite(upsert_block(mgr.load(), content))
        mgr.health_check()
        return ToolResult(output="Current Focus window updated.")


@registry.register
class ObservedHabitNoteTool(ToolBase):
    name = "observed_habit_note"
    description = (
        "Record a workflow habit the user stated or confirmed as a firm preference, phrased "
        "observationally ('User tends to …'). Subordinate — never a hard rule; promotion is user-gated."
    )
    parameters = {
        "type": "object",
        "properties": {
            "observation": {"type": "string", "description": "The habit, phrased observationally."}
        },
        "required": ["observation"],
    }

    async def execute(self, ctx: AgentContext, observation: str, **kwargs: Any) -> ToolResult:
        if not active(ctx):
            return ToolResult(output=_DISABLED)
        from gabagent.config.paths import observed_habits_file
        from gabagent.stratum.observed import ObservedStore
        ObservedStore(observed_habits_file()).accrete(observation, origin="definitive", state="accreting")
        return ToolResult(output="Observed habit recorded (subordinate; promotion stays user-gated).")


@registry.register
class StratumStatusTool(ToolBase):
    name = "stratum_status"
    description = "Show the current Stratum state: the Current Focus window and the observed-habits store."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        if not active(ctx):
            return ToolResult(output=_DISABLED)
        from gabagent.config.paths import observed_habits_file
        from gabagent.session.memory import MemoryManager
        from gabagent.stratum.current_focus import extract_block
        from gabagent.stratum.observed import ObservedStore
        cf = extract_block(MemoryManager(ctx.cwd).load()) or "(no Current Focus window yet)"
        habits = ObservedStore(observed_habits_file()).load()
        lines = [cf, "", f"Observed habits: {len(habits)}"]
        for h in habits[:20]:
            lines.append(f"- [{h.state}] {h.heading} (hits={h.hits}, conflicts={h.conflict_count})")
        return ToolResult(output="\n".join(lines))
