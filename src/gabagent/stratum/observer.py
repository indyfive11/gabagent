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


# Stable rejection prefixes emitted by the permission/hook layer (loop.py) — a denial is a much
# stronger habit signal ("user always denies X") than an ordinary runtime error.
_DENIAL_PREFIXES = ("Blocked by permissions:", "Denied by user:", "Blocked by hook:")


def _file_path(arguments: str) -> str | None:
    try:
        import json
        return json.loads(arguments).get("file_path") or None
    except Exception:
        return None


def capture(ctx: AgentContext, tool_calls: list[ToolCallSpec], results: list[ToolResult]) -> None:
    """Record deterministic signals for one tool turn. Never raises, never touches disk."""
    try:
        signals = ctx.stratum_signals
        seen = ctx.stratum_seen_calls
        repeat_t = int(getattr(ctx.config.stratum, "repeat_signal_threshold", 3))
        churn_t = int(getattr(ctx.config.stratum, "edit_churn_threshold", 3))
        for tc, result in zip(tool_calls, results):
            if result is None:
                continue
            if not result.success:
                err = result.error or ""
                if any(err.startswith(p) for p in _DENIAL_PREFIXES):
                    _push(signals, {"kind": "denied", "tool": tc.name, "detail": err[:200]})
                else:
                    _push(signals, {"kind": "tool_error", "tool": tc.name, "detail": err[:200]})
            # identical (tool, args) repeated — a redo/oscillation marker
            key = f"{tc.name}\x1f{tc.arguments}"
            n = seen.get(key, 0) + 1
            seen[key] = n
            if n == repeat_t:  # emit once, exactly at the threshold crossing
                _push(signals, {"kind": "repeat", "tool": tc.name, "count": n})
            # same-file edit churn — the repeat check above misses it because the args differ each edit
            if tc.name in ("edit", "write"):
                path = _file_path(tc.arguments)
                if path:
                    ckey = f"edit\x1f{path}"
                    cn = seen.get(ckey, 0) + 1
                    seen[ckey] = cn
                    if cn == churn_t:
                        _push(signals, {"kind": "edit_churn", "tool": tc.name, "path": path, "count": cn})
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
    denied: dict[str, int] = {}
    repeats: dict[str, int] = {}
    churn: dict[str, int] = {}
    for s in signals:
        k = s.get("kind")
        if k == "tool_error":
            errors[s["tool"]] = errors.get(s["tool"], 0) + 1
        elif k == "denied":
            denied[s["tool"]] = denied.get(s["tool"], 0) + 1
        elif k == "repeat":
            repeats[s["tool"]] = max(repeats.get(s["tool"], 0), s.get("count", 0))
        elif k == "edit_churn":
            churn[s["path"]] = max(churn.get(s["path"], 0), s.get("count", 0))
    parts = []
    if denied:
        parts.append("user-denied/blocked: " + ", ".join(f"{k}×{v}" for k, v in denied.items()))
    if errors:
        parts.append("tool errors: " + ", ".join(f"{k}×{v}" for k, v in errors.items()))
    if repeats:
        parts.append("repeated identical calls: " + ", ".join(f"{k}×{v}" for k, v in repeats.items()))
    if churn:
        parts.append("same-file edit churn: " + ", ".join(f"{k}×{v}" for k, v in churn.items()))
    return "; ".join(parts)
