"""Desktop & window control for KDE Plasma (Wayland-friendly).

Window operations go through KWin's global shortcuts over DBus (`qdbus6 … invokeShortcut`),
which is the reliable Wayland path — no X11-only tools (wmctrl/xdotool) needed. Screenshots use
spectacle, quitting an app uses kquitapp6, and a couple of keyboard-driven actions use wtype.
Each command is published only if its specific binary is present, so the catalog adapts to the host.

Deliberately scoped (not "overdone"): the active window is the target for window ops (KWin shortcuts
act on the active window); there is no arbitrary keystroke injector and no window enumeration —
listing every open window reliably on Wayland needs a KWin script, left for a later pass.
"""
from __future__ import annotations
import shutil
from typing import TYPE_CHECKING

from gabagent.commands.model import Command, Slot, ShellBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_KGA = ["qdbus6", "org.kde.kglobalaccel", "/component/kwin",
        "org.kde.kglobalaccel.Component.invokeShortcut"]


def _kwin(shortcut: str) -> ShellBackend:
    """Invoke a named KWin global shortcut on the active window."""
    return ShellBackend(argv=[*_KGA, shortcut])


class DesktopProvider:
    id = "desktop"

    async def detect(self, ctx: AgentContext) -> bool:
        return any(shutil.which(b) for b in ("qdbus6", "spectacle", "kquitapp6", "wtype"))

    def commands(self, ctx: AgentContext) -> list[Command]:
        cmds: list[Command] = []

        if shutil.which("qdbus6"):
            cmds += [
                Command(id="window.maximize", domain="window", tier=1,
                        summary="Maximize the active window", backend=_kwin("Window Maximize"),
                        examples=["maximize this", "make this window full size"]),
                Command(id="window.minimize", domain="window", tier=1,
                        summary="Minimize the active window", backend=_kwin("Window Minimize"),
                        examples=["minimize this", "hide this window"]),
                Command(id="window.fullscreen", domain="window", tier=1,
                        summary="Make the active window fullscreen", backend=_kwin("Window Fullscreen"),
                        examples=["go fullscreen"]),
                Command(id="window.center", domain="window", tier=1,
                        summary="Center the active window", backend=_kwin("Window Move Center")),
                Command(id="window.to_other_screen", domain="window", tier=1,
                        summary="Move the active window to the next monitor",
                        backend=_kwin("Window One Screen to the Right"),
                        examples=["move this to my other monitor", "send it to the next screen"]),
                Command(id="window.switch", domain="window", tier=1,
                        summary="Switch to the next window", backend=_kwin("Walk Through Windows"),
                        examples=["switch windows", "next window"]),
                Command(id="window.close", domain="window", tier=2,
                        summary="Close the active window", confirm_template="Close the active window?",
                        backend=_kwin("Window Close"), examples=["close this window", "close this"]),
            ]

        if shutil.which("spectacle"):
            cmds.append(Command(
                id="desktop.screenshot", domain="desktop", tier=1,
                summary="Take a screenshot of the whole screen",
                # -f full screen, -b background (no editor), -n no notification; saves to the
                # default Pictures screenshot location.
                backend=ShellBackend(argv=["spectacle", "-f", "-b", "-n"]),
                examples=["take a screenshot", "capture the screen"]))

        if shutil.which("wtype"):
            cmds.append(Command(
                id="desktop.close_tab", domain="desktop", tier=2,
                summary="Close the current browser tab (Ctrl+W)",
                confirm_template="Close the current tab?",
                backend=ShellBackend(argv=["wtype", "-M", "ctrl", "w", "-m", "ctrl"]),
                examples=["close this tab", "close the tab"]))

        if shutil.which("kquitapp6"):
            cmds.append(Command(
                id="desktop.quit_app", domain="desktop", tier=2,
                summary="Quit an application by name",
                confirm_template="Quit {app}?",
                backend=ShellBackend(argv=["kquitapp6", "{app}"]),
                params=[Slot("app", "string", True,
                             description="application/service name, e.g. 'dolphin', 'org.kde.konsole'")],
                examples=["quit dolphin", "close firefox"]))

        return cmds


PROVIDER = DesktopProvider()
