"""Observer (Stratum §5.B) — cheap, deterministic, ZERO-IO per-turn signal capture on the tool-turn
hot path. It only LOGS raw seam-level events to an in-memory list on ``ctx``; the interpretation
(are these a habit?) is deferred to compact-prep (the model), off the hot path.

Truly deterministic seam signals only: a tool rejection / error, and an identical ``(tool, args)``
seen N times in a session (a redo/oscillation marker). "User corrections" and "behavioral patterns"
are NOT seam signals — they are model interpretations over this raw log.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext
    from gabagent.api.models import ToolCallSpec, ToolResult

_MAX_SIGNALS = 200  # in-memory guard; the log is flushed/interpreted at compact-prep


def capture(ctx: AgentContext, tool_calls: list[ToolCallSpec], results: list[ToolResult]) -> None:
    """Record deterministic signals for one tool turn. Never raises, never touches disk."""
    try:
        signals = ctx.stratum_signals
        seen = ctx.stratum_seen_calls
        threshold = int(getattr(ctx.config.stratum, "repeat_signal_threshold", 3))
        for tc, result in zip(tool_calls, results):
            if result is None:
                continue
            if not result.success:
                _push(signals, {"kind": "tool_error", "tool": tc.name,
                                "detail": (result.error or "")[:200]})
            key = f"{tc.name}\x1f{tc.arguments}"
            n = seen.get(key, 0) + 1
            seen[key] = n
            if n == threshold:  # emit once, exactly at the threshold crossing
                _push(signals, {"kind": "repeat", "tool": tc.name, "count": n})
    except Exception:
        pass


def _push(signals: list, sig: dict) -> None:
    signals.append(sig)
    if len(signals) > _MAX_SIGNALS:
        del signals[: len(signals) - _MAX_SIGNALS]


def summary(ctx: AgentContext) -> str:
    """A compact text digest of the raw signal log, for the compact-prep prompt. Empty if none."""
    signals = getattr(ctx, "stratum_signals", None) or []
    if not signals:
        return ""
    errors: dict[str, int] = {}
    repeats: dict[str, int] = {}
    for s in signals:
        if s.get("kind") == "tool_error":
            errors[s["tool"]] = errors.get(s["tool"], 0) + 1
        elif s.get("kind") == "repeat":
            repeats[s["tool"]] = max(repeats.get(s["tool"], 0), s.get("count", 0))
    parts = []
    if errors:
        parts.append("tool errors/rejections: " + ", ".join(f"{k}×{v}" for k, v in errors.items()))
    if repeats:
        parts.append("repeated identical calls: " + ", ".join(f"{k}×{v}" for k, v in repeats.items()))
    return "; ".join(parts)
