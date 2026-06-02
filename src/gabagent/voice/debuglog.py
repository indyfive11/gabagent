"""Opt-in per-turn brain-side debug log, keyed by the voice protocol session_id.

Captures the decisions the voice-agent side can't observe (meta-command matches, model
routing/escalation, tier classification + proof method + decision, tool outcomes, local
model unloads) so the two sides can co-debug a session by joining on `session_id`.

Off by default — only writes when ctx.voice_debug_path is set (config voice_debug_log).
Best-effort; never raises into the turn.
"""
from __future__ import annotations
import json
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def dlog(ctx: AgentContext, event: str, session: str | None = None, **fields) -> None:
    path = getattr(ctx, "voice_debug_path", None)
    if not path:
        return
    # Most events run inside a turn (ctx.voice_session is set); out-of-turn callers like /media/duck
    # pass the session_id explicitly.
    sid = session or getattr(getattr(ctx, "voice_session", None), "session_id", "")
    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "session": sid,
        "event": event,
        **{k: v for k, v in fields.items() if v is not None},
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
