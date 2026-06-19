"""Aria "HAL eye" status — the canonical state-file writer/reader (the cross-component contract).

A glowing-eye Conky panel reads Aria's current state from a tiny tmpfs file; this module is the
single source of truth for that file's PATH and SHAPE so every writer (gabagent TUI here, the
voice-agent front-end, anything else) produces an identical format the Conky/Lua reader can trust.

Contract (see ~/dev/aria-eye-indicator-DESIGN.md):
  path   : $XDG_RUNTIME_DIR/aria/state   (fallback ~/.local/state/aria/state)
  format : {"state": <STATE>, "level": 0.0-1.0, "ts": <unix float>}
  write  : atomic (temp file + os.replace) — a reader never sees a half-written line
  stale  : a reader treats missing / unparseable / older-than STALE_SECS as "off"

CLI (handy for driving the eye through its states while building the panel, before any live writer):
  python -m gabagent.aria_state thinking
  python -m gabagent.aria_state speaking 0.7
"""
from __future__ import annotations
import json
import os
import tempfile
import time
from pathlib import Path

# The semantic states. Appearance (color/pulse) is the panel's job, NOT encoded here.
#   "sleeping" — Aria is intentionally asleep (voice "go to sleep"), distinct from "off" which doubles
#   as dead/stale/crashed. A sleeping eye should read as dormant-but-alive, not dark-and-dead. The
#   voice layer (sole writer in voice mode) emits it on sleep-enter; it must KEEP refreshing it (or the
#   STALE_SECS failsafe below will decay it to "off" after 5s — that decay is acceptable as a fallback
#   but a steady "sleeping" render needs a heartbeat). Backward-compatible: a reader that doesn't know
#   "sleeping" falls back to dark, same as "off".
STATES = ("off", "idle", "listening", "thinking", "speaking", "error", "sleeping")
STALE_SECS = 5.0  # a reader past this with no fresh write should render "off"


def state_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".local" / "state")
    return Path(base) / "aria" / "state"


def set_state(state: str, level: float = 0.0) -> None:
    """Atomically write the current state. Never raises — a status file must not break the agent."""
    if state not in STATES:
        state = "idle"
    p = state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"state": state, "level": round(float(level), 3), "ts": time.time()})
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp, p)  # atomic on POSIX
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    except Exception:
        pass


def read_state(now: float | None = None) -> dict:
    """Read the current state for verification/tests. Missing / unparseable / stale → 'off'."""
    now = time.time() if now is None else now
    try:
        d = json.loads(state_path().read_text())
        ts = float(d.get("ts", 0))
        if now - ts > STALE_SECS:
            return {"state": "off", "level": 0.0, "ts": ts}
        return {"state": d.get("state", "off"), "level": float(d.get("level", 0.0)), "ts": ts}
    except Exception:
        return {"state": "off", "level": 0.0, "ts": 0.0}


if __name__ == "__main__":  # pragma: no cover
    import sys
    st = sys.argv[1] if len(sys.argv) > 1 else "idle"
    lvl = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    set_state(st, lvl)
    print(f"aria state → {st} ({lvl}) @ {state_path()}")
