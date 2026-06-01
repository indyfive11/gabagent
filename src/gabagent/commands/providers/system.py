"""System controls: volume / brightness (Tier 1 — harmless, reversible) and power
(Tier 3 — keyboard). Each command is published only if its specific binary is present, so
the catalog adapts to the host (pactl vs wpctl, brightnessctl, systemctl).
"""
from __future__ import annotations
import shutil
from typing import TYPE_CHECKING

from gabagent.commands.model import Command, ShellBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def _sh(*args) -> ShellBackend:
    return ShellBackend(argv=list(args))


class SystemProvider:
    id = "system"

    async def detect(self, ctx: AgentContext) -> bool:
        return any(shutil.which(b) for b in ("pactl", "wpctl", "brightnessctl", "systemctl"))

    def commands(self, ctx: AgentContext) -> list[Command]:
        cmds: list[Command] = []

        if shutil.which("pactl"):
            s = "@DEFAULT_SINK@"
            cmds += [
                Command(id="system.volume_up", domain="system", tier=1, summary="Turn the system volume up",
                        backend=_sh("pactl", "set-sink-volume", s, "+10%"), examples=["turn it up", "louder"]),
                Command(id="system.volume_down", domain="system", tier=1, summary="Turn the system volume down",
                        backend=_sh("pactl", "set-sink-volume", s, "-10%"), examples=["turn it down", "quieter"]),
                Command(id="system.mute", domain="system", tier=1, summary="Toggle system mute",
                        backend=_sh("pactl", "set-sink-mute", s, "toggle")),
            ]
        elif shutil.which("wpctl"):
            s = "@DEFAULT_AUDIO_SINK@"
            cmds += [
                Command(id="system.volume_up", domain="system", tier=1, summary="Turn the system volume up",
                        backend=_sh("wpctl", "set-volume", s, "0.1+")),
                Command(id="system.volume_down", domain="system", tier=1, summary="Turn the system volume down",
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
                Command(id="system.suspend", domain="system", tier=3, summary="Suspend the computer",
                        backend=_sh("systemctl", "suspend"), examples=["go to sleep", "suspend"]),
                Command(id="system.reboot", domain="system", tier=3, summary="Reboot the computer",
                        backend=_sh("systemctl", "reboot")),
                Command(id="system.poweroff", domain="system", tier=3, summary="Power off the computer",
                        backend=_sh("systemctl", "poweroff"), examples=["shut down"]),
            ]
        return cmds


PROVIDER = SystemProvider()
