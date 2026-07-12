"""System controls: volume / brightness (Tier 1 — harmless, reversible) and power
(Tier 3 — keyboard). Each command is published only if its specific binary is present, so
the catalog adapts to the host (pactl vs wpctl, brightnessctl, systemctl).
"""
from __future__ import annotations
import shutil
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, PyBackend, ShellBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_FIX_AUDIO_FLOOR = 50   # P1: "can't hear" recovery raises the default sink to at least this % if below


def _sh(*args) -> ShellBackend:
    return ShellBackend(argv=list(args))


async def fix_audio(ctx: AgentContext) -> ToolResult:
    """Active audio recovery for "I can't hear" (P1). Instead of the blind mute TOGGLE (system.mute),
    which can MUTE an already-live sink, this idempotently asserts the DEFAULT SINK is unmuted and at
    or above a floor, changing only what's actually wrong: unmute if muted, raise to the floor ONLY if
    below it (the level nudge is conditional; unmute is the decisive part).

    INVARIANT: writes ONLY the default sink, never a sink-input. The voice front-end (VAC) owns Aria's
    TTS sink-input via a pid-matched pin loop; the sink and sink-input volumes COMPOSITE (multiply), so
    a 50% default-sink floor stacks on top of VAC's 60% sink-input pin — this must never touch a
    sink-input or the two writers would collide. User-invoked only (never auto-fired from the read-side
    audibility probe). pactl-only; a wpctl-only host is a documented follow-up."""
    from gabagent.voice.ducking import _default_sink_name, _run_pactl, _sink_mute_volume
    sink = await _default_sink_name()
    if not sink:
        return ToolResult(output="", error="I couldn't find your audio output to fix it.")
    muted, vol = await _sink_mute_volume(sink)
    did: list[str] = []
    if muted is True:
        rc, _ = await _run_pactl("set-sink-mute", sink, "0")   # idempotent unmute (0), never a toggle
        if rc == 0:
            did.append("unmuted your speakers")
    if vol is not None and vol < _FIX_AUDIO_FLOOR:
        rc, _ = await _run_pactl("set-sink-volume", sink, f"{_FIX_AUDIO_FLOOR}%")
        if rc == 0:
            did.append(f"set the volume to {_FIX_AUDIO_FLOOR}%")
    if did:
        return ToolResult(output=f"I've {' and '.join(did)}.")
    if muted is False:
        return ToolResult(output="Your speakers are already unmuted and turned up — the sound should "
                                 "be working. If you still can't hear, it may be the app or the wrong "
                                 "output device.")
    return ToolResult(output="", error="I couldn't read your audio device to check it.")


class SystemProvider:
    id = "system"

    async def detect(self, ctx: AgentContext) -> bool:
        return any(shutil.which(b) for b in ("pactl", "wpctl", "brightnessctl", "systemctl"))

    def commands(self, ctx: AgentContext) -> list[Command]:
        cmds: list[Command] = []

        if shutil.which("pactl"):
            s = "@DEFAULT_SINK@"
            cmds += [
                Command(id="system.volume_up", domain="system", tier=1, featured=True, summary="Turn the system volume up",
                        backend=_sh("pactl", "set-sink-volume", s, "+10%"), examples=["turn it up", "louder"]),
                Command(id="system.volume_down", domain="system", tier=1, featured=True, summary="Turn the system volume down",
                        backend=_sh("pactl", "set-sink-volume", s, "-10%"), examples=["turn it down", "quieter"]),
                Command(id="system.mute", domain="system", tier=1, summary="Toggle system mute",
                        backend=_sh("pactl", "set-sink-mute", s, "toggle")),
                Command(id="system.fix_audio", domain="system", tier=1, featured=True,
                        summary="Recover audio when the user can't hear: idempotently unmute the "
                                "default output and raise it to a floor if too low. Use for 'I can't "
                                "hear', 'no sound', 'fix the audio' — NOT the blind mute toggle.",
                        backend=PyBackend(ref="gabagent.commands.providers.system:fix_audio"),
                        examples=["I can't hear you", "I can't hear anything", "there's no sound",
                                  "fix the audio"]),
            ]
        elif shutil.which("wpctl"):
            s = "@DEFAULT_AUDIO_SINK@"
            cmds += [
                Command(id="system.volume_up", domain="system", tier=1, featured=True, summary="Turn the system volume up",
                        backend=_sh("wpctl", "set-volume", s, "0.1+")),
                Command(id="system.volume_down", domain="system", tier=1, featured=True, summary="Turn the system volume down",
                        backend=_sh("wpctl", "set-volume", s, "0.1-")),
                Command(id="system.mute", domain="system", tier=1, summary="Toggle system mute",
                        backend=_sh("wpctl", "set-mute", s, "toggle")),
            ]

        if shutil.which("brightnessctl"):
            cmds += [
                Command(id="system.brightness_up", domain="system", tier=1, summary="Increase screen brightness",
                        backend=_sh("brightnessctl", "set", "+10%")),
                Command(id="system.brightness_down", domain="system", tier=1, summary="Decrease screen brightness",
                        backend=_sh("brightnessctl", "set", "10%-")),
            ]

        if shutil.which("systemctl"):
            cmds += [
                # NB: deliberately NO "go to sleep" example — that phrase is the voice layer's
                # sleep control (pause Aria), which it consumes before /respond. Keeping it here
                # risked an ASR-mangled "go to sleep" slipping through to the brain and matching
                # this → systemctl suspend → suspending the whole PC. "suspend" is unambiguous.
                Command(id="system.suspend", domain="system", tier=3, summary="Suspend the computer",
                        backend=_sh("systemctl", "suspend"), examples=["suspend"]),
                Command(id="system.reboot", domain="system", tier=3, summary="Reboot the computer",
                        backend=_sh("systemctl", "reboot")),
                Command(id="system.poweroff", domain="system", tier=3, summary="Power off the computer",
                        backend=_sh("systemctl", "poweroff"), examples=["shut down"]),
            ]
        return cmds


PROVIDER = SystemProvider()
