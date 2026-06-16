"""Countdown timers (G2) — a built-in, always-available capability. Tier 1 (setting,
listing, and cancelling a timer are all harmless/reversible). State + expiry firing live in
voice/timers.py; this module is just the command surface (set / list / cancel).

The fire→ring half is intentionally absent here — a fired timer reaches the voice side over a
proactive brain→voice channel that is still a co-design contract (see PLAN_brain_roadmap_impl_*).
Until that lands, the expiry ticker only logs the fire; these commands are fully functional.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, PyBackend, Slot
from gabagent.voice import timers as _t

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_REF = "gabagent.commands.providers.timer:"


class TimerProvider:
    id = "timer"

    async def detect(self, ctx: AgentContext) -> bool:
        return True  # built-in; no device/binary to probe

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(
                id="timer.set", domain="timer", tier=1, featured=True,
                summary="Set a countdown timer",
                backend=PyBackend(ref=_REF + "set_timer"),
                params=[
                    Slot("seconds", "integer", False, description="duration in seconds (preferred; convert spoken durations to seconds)"),
                    Slot("duration", "string", False, description="natural duration if seconds not given, e.g. '10 minutes'"),
                    Slot("label", "string", False, description="optional name, e.g. 'pasta'"),
                ],
                examples=["set a timer for 10 minutes", "set a 5 minute pasta timer", "timer for 90 seconds"],
            ),
            Command(
                id="timer.list", domain="timer", tier=1,
                summary="List active timers and time remaining",
                backend=PyBackend(ref=_REF + "list_timers"),
                examples=["how long left on my timer", "list my timers", "what timers do i have"],
            ),
            Command(
                id="timer.cancel", domain="timer", tier=1,
                summary="Cancel a timer",
                backend=PyBackend(ref=_REF + "cancel_timer"),
                params=[Slot("label", "string", False, description="which timer to cancel; omit if there is only one")],
                examples=["cancel my timer", "stop the pasta timer", "clear my timers"],
            ),
        ]


PROVIDER = TimerProvider()


# -- backends: async fn(ctx, **slots) -> ToolResult -------------------------

def _label_suffix(label: str | None) -> str:
    return f" for {label}" if label else ""


async def set_timer(ctx, seconds=None, duration=None, label=None) -> ToolResult:
    vs = getattr(ctx, "voice_session", None)
    if vs is None:
        return ToolResult(output="", error="Timers are only available in voice mode.")

    secs = int(seconds) if seconds is not None else _t.parse_duration(duration)
    if secs is None:
        return ToolResult(output="", error="Tell me how long, e.g. 'set a timer for 10 minutes'.")
    if secs <= 0:
        return ToolResult(output="", error="That timer is too short.")
    if secs > _t.MAX_DURATION_SECS:
        return ToolResult(output="", error="I can only set timers up to 24 hours.")
    if len(_t.active(vs)) >= _t.MAX_TIMERS:
        return ToolResult(output="", error=f"You already have {_t.MAX_TIMERS} timers; cancel one first.")

    label = (label or "").strip() or None
    _t.add_timer(vs, secs, label)
    return ToolResult(output=f"Timer set for {_t.human(secs)}{_label_suffix(label)}.")


async def list_timers(ctx) -> ToolResult:
    vs = getattr(ctx, "voice_session", None)
    if vs is None:
        return ToolResult(output="", error="Timers are only available in voice mode.")
    ts = _t.active(vs)
    if not ts:
        return ToolResult(output="You have no timers set.")
    parts = [
        f"{_t.human(_t.remaining_secs(t))} left" + (f" on {t.label}" if t.label else "")
        for t in ts
    ]
    return ToolResult(output="You have " + _t.join_phrase(parts) + ".")


async def cancel_timer(ctx, label=None) -> ToolResult:
    vs = getattr(ctx, "voice_session", None)
    if vs is None:
        return ToolResult(output="", error="Timers are only available in voice mode.")
    label = (label or "").strip() or None
    status, payload = _t.cancel(vs, label)
    if status == "none":
        if label:
            return ToolResult(output=f"I don't have a timer called {label}.")
        return ToolResult(output="You have no timers to cancel.")
    if status == "ambiguous":
        names = _t.join_phrase([t.label or "an unnamed timer" for t in payload])
        return ToolResult(output=f"You have several timers ({names}). Which should I cancel?")
    return ToolResult(output=f"Cancelled the {payload.label} timer." if payload.label else "Cancelled your timer.")
