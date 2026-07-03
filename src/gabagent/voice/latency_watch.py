"""Latency self-test → auto-Turbo (#6) — the brain watches real arya ttft and, when the cloud is the
DOMINANT cause of slow turns, offers to switch command turns to the fast (Claude) rung; "yes" toggles
Turbo. When arya recovers it offers to switch back.

Design (consensus with VAC, 2026-06-25; per-round sampling added 2026-07-03):
  - Detection is PASSIVE — off EVERY arya round's ttft of real turns (round-0 and any tool-loop round). A
    round's ttft is pure model latency (tool exec falls BETWEEN rounds), so it's an arya-only sample: a slow
    round-1+ or a bad-cloud multi-round turn is caught, which round-0-only sampling missed. No idle probing.
  - Tool time is therefore never counted as arya latency (that's what the attribution ratio in record() was a
    proxy for — a fast-arya turn that felt slow from a long Tidal/command call does NOT trip). A movie that is
    slow only from MANY normal-speed rounds is also (correctly) not a trip: that's round-count, not cloud
    latency — offering Turbo there would mislabel a healthy cloud.
  - Recovery (while in Turbo, where commands route to Claude so there's no passive arya signal) uses a
    bounded ACTIVE probe — and self-suppresses if any room produced a recent real arya sample (the global
    sample buffer IS the "is any room feeding arya" signal, so no room registry is needed).
  - Everything is OFF unless voice_latency_watch is set (it auto-spends). All thresholds are config.

Kept OUT of the spine: turn.py calls record() / end_of_turn() / resolve_offer() as thin hooks; the policy
lives here. Pending-offer + cooldown state is per-session; arya samples are global (one shared cloud)."""
from __future__ import annotations

import re
import time
from collections import deque
from statistics import median

# Global arya-latency samples — arya is ONE shared cloud backend, so its latency is global, not per-room.
# (mono_ts, arya_ttft_ms, dominant) where dominant = arya was ≥attribution of the turn's total time.
_SAMPLES: deque = deque(maxlen=32)
_LAST_OFFER: dict[tuple, float] = {}   # (session_id, kind) → mono of last offer (per-direction anti-nag)
_PROBE = {"day": None, "count": 0, "last_mono": 0.0}

# Affirmation set for a pending offer. Deliberately NO directional phrases ("turn it on/off") — those would
# mis-match a turbo_OFF offer ("turn it on" is not a yes to "switch Turbo off?"). And no bare "do it"
# (too frequent in normal speech — VAC's Q3). The offer is a yes/no question; these are the yes-words.
_AFFIRM = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|go ahead|go for it|please do|do that|"
    r"sounds good|definitely|absolutely)\b", re.I)


def _cfg(ctx, name, default):
    return getattr(ctx.config, name, default)


def enabled(ctx) -> bool:
    return bool(_cfg(ctx, "voice_latency_watch", False))


# -- sampling ---------------------------------------------------------------

def record(ctx, arya_ttft_ms: int, turn_total_ms: int) -> None:
    """Feed one real arya turn: its round-0 ttft and the turn's total time. `dominant` marks whether arya
    (not a long tool/command call) drove the slowness — the offer's attribution gate. No-op when disabled."""
    if not enabled(ctx) or arya_ttft_ms is None:
        return
    attr = float(_cfg(ctx, "voice_latency_attribution", 0.6))
    total = max(1, int(turn_total_ms or 0))
    dominant = arya_ttft_ms >= attr * total
    _SAMPLES.append((time.monotonic(), int(arya_ttft_ms), dominant))


def _recent(n):
    return list(_SAMPLES)[-n:]


def degraded(ctx) -> bool:
    """Arya is the dominant slowness AND either a full window of arya-dominated turns exceeds the median
    ceiling, or any single arya-dominated turn exceeded the hard ceiling."""
    if not enabled(ctx):
        return False
    win = max(1, int(_cfg(ctx, "voice_latency_window", 3)))
    hard = int(_cfg(ctx, "voice_latency_hard_ceiling_ms", 15000))
    ceil = int(_cfg(ctx, "voice_latency_ceiling_ms", 8000))
    recent = _recent(win)
    if not recent:
        return False
    if any(d and t >= hard for _, t, d in recent):
        return True
    dom = [t for _, t, d in recent if d]
    if len(dom) < win:
        return False
    return median(dom) > ceil


def recovered(ctx) -> bool:
    """Arya is healthy again: the last `window` samples are all below the floor (hysteresis vs ceiling)."""
    if not enabled(ctx):
        return False
    win = max(1, int(_cfg(ctx, "voice_latency_window", 3)))
    floor = int(_cfg(ctx, "voice_latency_floor_ms", 4000))
    recent = _recent(win)
    if len(recent) < win:
        return False
    return all(t < floor for _, t, _ in recent)


# -- offer state (per session) ----------------------------------------------

def pending(ctx) -> dict | None:
    """The live pending offer for this session, or None (also clears an expired one)."""
    p = getattr(ctx, "pending_turbo_offer", None)
    if not p:
        return None
    if time.monotonic() >= p.get("expires", 0):
        ctx.pending_turbo_offer = None
        return None
    return p


def _set_pending(ctx, kind: str) -> dict:
    ttl = float(_cfg(ctx, "voice_latency_offer_ttl_secs", 60.0))
    p = {"kind": kind, "expires": time.monotonic() + ttl, "ttl_secs": ttl}
    ctx.pending_turbo_offer = p
    return p


def _in_cooldown(ctx, session_id: str, kind: str) -> bool:
    """Per-(session, DIRECTION) anti-nag: an 'on' offer must not block an 'off' offer (or vice versa) —
    they're independent events. Keying by kind prevents that cross-block."""
    cd = float(_cfg(ctx, "voice_latency_offer_cooldown_secs", 600.0))
    return (time.monotonic() - _LAST_OFFER.get((session_id, kind), -1e18)) < cd


_OFFER_ON = ("I'm having some latency trouble with my cloud service. "
             "Want me to switch to Turbo mode? Shall I turn it on now?")
_OFFER_OFF = "My cloud service is back to normal — want me to switch Turbo off?"
_ACCEPT_ON = "Turbo mode on — this runs on your Claude API account and costs slightly more."
_ACCEPT_OFF = "Back to normal — Turbo off."


def maybe_offer(ctx, session_id: str) -> dict | None:
    """End-of-turn decision: should Aria offer a Turbo toggle? Returns {kind,text,hold,ttl_secs} to speak
    (and arms the pending offer + cooldown) or None. Mutually exclusive cases:
      - NOT in Turbo, arya degraded, not cooled-down, nothing pending → offer to turn ON.
      - in Turbo, arya recovered → offer to turn OFF (recovery; no cooldown gate — recovery is good news).
    """
    if not enabled(ctx) or pending(ctx) is not None:
        return None
    ttl = float(_cfg(ctx, "voice_latency_offer_ttl_secs", 60.0))
    in_turbo = bool(getattr(ctx, "turbo_commands", False))
    if not in_turbo and degraded(ctx) and not _in_cooldown(ctx, session_id, "turbo_on"):
        _set_pending(ctx, "turbo_on")
        _LAST_OFFER[(session_id, "turbo_on")] = time.monotonic()
        return {"kind": "turbo_on", "text": _OFFER_ON, "hold": True, "ttl_secs": ttl}
    if in_turbo and recovered(ctx) and not _in_cooldown(ctx, session_id, "turbo_off"):
        _set_pending(ctx, "turbo_off")
        _LAST_OFFER[(session_id, "turbo_off")] = time.monotonic()
        return {"kind": "turbo_off", "text": _OFFER_OFF, "hold": True, "ttl_secs": ttl}
    return None


def resolve_offer(ctx, user_text: str) -> str | None:
    """Pre-route: if an offer is pending and the user affirmed, toggle Turbo and return the spoken line
    (no model call). If pending but NOT an affirmation, drop the offer (they moved on) and return None so
    the turn proceeds normally. Returns None when nothing is pending."""
    p = pending(ctx)
    if p is None:
        return None
    if not _AFFIRM.match(user_text or ""):
        ctx.pending_turbo_offer = None      # they said something else → offer lapses, handle normally
        return None
    ctx.pending_turbo_offer = None
    if p["kind"] == "turbo_on":
        ctx.turbo_commands = True
        return _ACCEPT_ON
    ctx.turbo_commands = False
    return _ACCEPT_OFF


# -- recovery probe (active, bounded) ---------------------------------------

def _probe_due(ctx) -> bool:
    """A recovery probe is due only while in Turbo, with no recent real arya sample (no room feeding arya),
    respecting the cadence and the per-day budget."""
    if not enabled(ctx) or not getattr(ctx, "turbo_commands", False):
        return False
    cadence = float(_cfg(ctx, "voice_latency_probe_secs", 75.0))
    now = time.monotonic()
    if _SAMPLES and (now - _SAMPLES[-1][0]) < cadence:
        return False                         # a room produced a recent arya sample → no probe needed
    if (now - _PROBE["last_mono"]) < cadence:
        return False
    today = time.strftime("%Y-%m-%d")
    if _PROBE["day"] != today:
        _PROBE["day"], _PROBE["count"] = today, 0
    return _PROBE["count"] < int(_cfg(ctx, "voice_latency_probe_daily_max", 40))


async def maybe_probe(ctx) -> None:
    """Fire ONE bounded arya probe (a throwaway completion) to detect recovery while in Turbo, recording
    its ttft as a sample so recovered()/maybe_offer() can see it. Best-effort; never raises out."""
    if not _probe_due(ctx):
        return
    _PROBE["last_mono"] = time.monotonic()
    _PROBE["count"] += 1
    rt = getattr(ctx.config, "router", None)
    model = (getattr(rt, "simple_model", None) if rt else None) or "arya"
    t = time.monotonic()
    try:
        from gabagent.api.models import ChatMessage
        await ctx.client.complete_simple([ChatMessage(role="user", content="hi")], model=model)
        ttft = int((time.monotonic() - t) * 1000)
        _SAMPLES.append((time.monotonic(), ttft, True))
        try:
            from gabagent.voice.debuglog import dlog
            dlog(ctx, "latency_probe", ttft_ms=ttft)
        except Exception:
            pass
    except Exception:
        pass


def _reset_for_test():
    """Test helper — clear global state between cases."""
    _SAMPLES.clear()
    _LAST_OFFER.clear()
    _PROBE.update(day=None, count=0, last_mono=0.0)
