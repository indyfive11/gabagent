"""Countdown timers — brain-side state + expiry firing (G2, brain-solo core).

A timer is per-voice-session state (`VoiceSession.timers`). The `timer.*` command provider
(commands/providers/timer.py) creates/lists/cancels them; an independent ~1 Hz async `ticker`
(started by serve_voice) fires the ones that come due.

DELIVERY IS HELD: when a timer fires the ticker currently only `dlog`s it — it does NOT push
anything to the voice side, because the brain→voice proactive channel that would carry a ring
is still a co-design contract (see PLAN_brain_roadmap_impl_*). So this module is inert on the
voice side: set a timer, watch it fire in the brain debug log, nothing audible. Wiring the
`timer_fired` event through the proactive channel is the follow-up once that contract lands.

All time math takes an injectable `now` (epoch seconds) so the logic is unit-testable with a
faked clock — the ticker is the only place that reads the real wall clock.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any

# Caps (kept as module constants for the core; can move to config later if needed).
MAX_TIMERS = 10
MAX_DURATION_SECS = 24 * 3600  # 24h
TICK_INTERVAL_SECS = 1.0


@dataclass
class Timer:
    id: str
    label: str | None
    expires_epoch: float
    set_secs: int
    created_epoch: float


# -- state access (tolerant so a lightweight test stub works too) -----------

def _timers(vs: Any) -> dict[str, Timer]:
    t = getattr(vs, "timers", None)
    if t is None:
        t = {}
        vs.timers = t
    return t


def add_timer(vs: Any, set_secs: int, label: str | None = None, now: float | None = None) -> Timer:
    now = time.time() if now is None else now
    n = getattr(vs, "_timer_n", 0) + 1
    vs._timer_n = n
    t = Timer(
        id=f"t{n}",
        label=label or None,
        expires_epoch=now + set_secs,
        set_secs=int(set_secs),
        created_epoch=now,
    )
    _timers(vs)[t.id] = t
    return t


def active(vs: Any) -> list[Timer]:
    """All pending timers, soonest-to-fire first."""
    return sorted(_timers(vs).values(), key=lambda t: t.expires_epoch)


def remaining_secs(t: Timer, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(0, math.ceil(t.expires_epoch - now))


def cancel(vs: Any, label: str | None = None) -> tuple[str, Any]:
    """Cancel a timer. Returns one of:
      ("ok", Timer)        — cancelled it
      ("none", None)       — nothing to cancel / no label match
      ("ambiguous", list)  — >1 timer and no label to disambiguate
    """
    ts = list(_timers(vs).values())
    if not ts:
        return ("none", None)
    if label:
        wanted = label.strip().lower()
        matches = [t for t in ts if (t.label or "").lower() == wanted]
        if not matches:
            return ("none", None)
        _timers(vs).pop(matches[0].id, None)
        return ("ok", matches[0])
    if len(ts) == 1:
        only = ts[0]
        _timers(vs).pop(only.id, None)
        return ("ok", only)
    return ("ambiguous", active(vs))


def pop_due(vs: Any, now: float) -> list[Timer]:
    """Remove and return all timers due at `now` (soonest first)."""
    due = sorted(
        (t for t in _timers(vs).values() if t.expires_epoch <= now),
        key=lambda t: t.expires_epoch,
    )
    for t in due:
        _timers(vs).pop(t.id, None)
    return due


# -- duration parsing + human phrasing --------------------------------------

# No trailing \b: a compact form like "1h30m" has no word boundary between "h" and "3", and the
# required leading digit already prevents matching inside plain words.
_DUR_RE = re.compile(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)", re.IGNORECASE)
_MULT = {"h": 3600, "m": 60, "s": 1}  # keyed by first letter of the matched unit


def parse_duration(text: str | None) -> int | None:
    """Parse '10 minutes', '1h30m', '90 sec', '2 min 30 s' → total seconds. None if no unit found."""
    if text is None:
        return None
    total = 0
    found = False
    for m in _DUR_RE.finditer(str(text)):
        total += int(m.group(1)) * _MULT[m.group(2)[0].lower()]
        found = True
    return total if found else None


def human(secs: int) -> str:
    """Speakable duration: '10 minutes', '1 hour 5 minutes', '30 seconds'. Seconds dropped when hours present."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts) or "0 seconds"


def ring_phrase(t: Timer) -> str:
    """Speakable line for a fired timer — verbatim text the deferred-announce channel will voice."""
    dur = human(t.set_secs)
    if t.label:
        return f"Your {t.label} timer is up — that was {dur}."
    return f"Your {dur} timer is up."


def join_phrase(parts: list[str]) -> str:
    """'a' | 'a and b' | 'a, b, and c'."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


# -- firing -----------------------------------------------------------------

async def fire_due(ctx: Any, sessions: dict, now: float | None = None) -> list[Timer]:
    """Scan every session, fire any due timers: dlog them AND enqueue a spoken ring onto the deferred-
    announce channel (voice/announce_store.py — the steady, sleep-independent brain→voice channel that
    finally makes a timer audible). The ring prefers its owning session (originating-first), but the
    liveness-leased store delivers it to whatever Rob device is present. Returns the fired timers."""
    now = time.time() if now is None else now
    from gabagent.voice.debuglog import dlog
    from gabagent.voice import announce_store

    fired: list[Timer] = []
    for vs in list(sessions.values()):
        sid = getattr(vs, "session_id", "") or ""
        for t in pop_due(vs, now):
            dlog(ctx, "timer_fired", session=sid, id=t.id, label=t.label or "", set_secs=t.set_secs)
            try:
                announce_store.enqueue(
                    ring_phrase(t),
                    job_id=f"timer-{sid}-{t.id}-{int(t.expires_epoch)}",
                    source="timer",
                    preferred_session=sid or None,
                    now=now,
                )
            except Exception:  # a store hiccup must never kill the tick loop
                pass
            fired.append(t)
    return fired


async def ticker(ctx: Any, sessions: dict, interval: float = TICK_INTERVAL_SECS) -> None:
    """Independent ~1 Hz loop that fires due timers. Started by serve_voice; cancelled on shutdown.

    Deliberately NOT riding the /media/state poll: a timer set in silence has no media to poll
    against, so timer firing must not depend on VAC's media-driven poll cadence."""
    import asyncio

    while True:
        try:
            await fire_due(ctx, sessions)
        except Exception:  # never let a bad tick kill the loop
            pass
        await asyncio.sleep(interval)
