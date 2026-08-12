"""Session injection (Stratum §4/§7) — assembles the TRANSIENT context Stratum adds to a turn: the
ranked subordinate observed-habits block + a Current-Focus size reminder.

Returned as a string the loop appends to ``system_content`` *in memory* at request assembly — it is
NEVER persisted to the session JSONL (persisting would stack duplicate/stale blocks across
--continue/--resume). It lands AFTER the frozen base prompt, so it stays subordinate and does not
disturb the cached prefix.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from gabagent.stratum import active

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def session_block(ctx: AgentContext) -> str:
    """The transient Stratum tail segment for this turn. Empty string when inactive or nothing to
    add (so an unconfigured/disabled install is byte-identical). Prefixed with a blank-line separator
    when non-empty so the caller can append it directly."""
    if not active(ctx):
        return ""
    try:
        from gabagent.config.paths import observed_habits_file
        from gabagent.session.memory import MemoryManager
        from gabagent.stratum.current_focus import size_reminder
        from gabagent.stratum.observed import ObservedStore

        cfg = ctx.config.stratum
        parts: list[str] = []

        habits = ObservedStore(observed_habits_file()).render_block(halflife_days=cfg.tier05_halflife_days)
        if habits:
            parts.append(habits)

        memory_text = MemoryManager(ctx.cwd).load()
        reminder = size_reminder(memory_text, cfg)
        if reminder:
            parts.append(reminder)

        if not parts:
            return ""
        return "\n\n" + "\n\n".join(parts)
    except Exception:
        return ""
