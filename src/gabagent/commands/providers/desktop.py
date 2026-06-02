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
import asyncio
import json
import os
import re
import shutil
import tempfile
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, Slot, ShellBackend, PyBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_KGA = ["qdbus6", "org.kde.kglobalaccel", "/component/kwin",
        "org.kde.kglobalaccel.Component.invokeShortcut"]
_SCRIPTING = ["qdbus6", "org.kde.KWin", "/Scripting"]
_KWIN_PLUGIN = "gabagent_winmove"


def _kwin(shortcut: str) -> ShellBackend:
    """Invoke a named KWin global shortcut on the active window."""
    return ShellBackend(argv=[*_KGA, shortcut])


# -- KWin scripting (Wayland-safe window→output targeting) ------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# Move the active window to the largest output (by logical area).
_JS_LARGEST = (
    "(function(){var w=workspace.activeWindow;if(!w)return;var s=workspace.screens;"
    "if(!s||!s.length)return;var b=s[0];for(var i=1;i<s.length;i++){"
    "if(s[i].geometry.width*s[i].geometry.height>b.geometry.width*b.geometry.height)b=s[i];}"
    "workspace.sendClientToScreen(w,b);})();"
)
# Move the active window to a named output (%TNAME%) or 1-based index (%TI%).
_JS_TO_SCREEN = (
    "(function(){var w=workspace.activeWindow;if(!w)return;var tn=%TNAME%;var ti=%TI%;"
    "var s=workspace.screens;for(var i=0;i<s.length;i++){"
    "if(s[i].name===tn||(i+1)===ti){workspace.sendClientToScreen(w,s[i]);return;}}})();"
)


async def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    """Run a command (no shell); return (returncode, stdout)."""
    try:
        p = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
        return p.returncode or 0, out.decode(errors="replace")
    except Exception:
        return 1, ""


async def _run_kwin_script(js: str) -> bool:
    """Load + run a one-shot KWin script, then unload it. The whole-token argv (and the JSON-
    encoded values inside the JS) keep it injection-safe."""
    fd, path = tempfile.mkstemp(suffix=".js", prefix="gabagent_kwin_")
    try:
        os.write(fd, js.encode())
        os.close(fd)
        await _run([*_SCRIPTING, "org.kde.kwin.Scripting.unloadScript", _KWIN_PLUGIN])
        rc, _out = await _run([*_SCRIPTING, "org.kde.kwin.Scripting.loadScript", path, _KWIN_PLUGIN])
        if rc != 0:
            return False
        await _run([*_SCRIPTING, "org.kde.kwin.Scripting.start"])
        await asyncio.sleep(0.2)  # let the script execute before unloading
        await _run([*_SCRIPTING, "org.kde.kwin.Scripting.unloadScript", _KWIN_PLUGIN])
        return True
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


async def _kscreen_outputs() -> list[dict]:
    """Parse `kscreen-doctor -o` into [{name,width,height,primary}] for enabled outputs."""
    rc, out = await _run(["kscreen-doctor", "-o"])
    if rc != 0:
        return []
    text = _ANSI.sub("", out)
    screens: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = re.match(r"\s*Output:\s+\d+\s+(\S+)", line)
        if m:
            cur = {"name": m.group(1), "primary": False, "width": 0, "height": 0}
            screens.append(cur)
            continue
        if cur is None:
            continue
        if re.search(r"\bpriority\s+1\b", line):
            cur["primary"] = True
        g = re.search(r"Geometry:\s+-?\d+,-?\d+\s+(\d+)x(\d+)", line)
        if g:
            cur["width"], cur["height"] = int(g.group(1)), int(g.group(2))
    return [s for s in screens if s["width"]]


# -- backend callables -----------------------------------------------------

async def list_screens(ctx) -> ToolResult:
    screens = await _kscreen_outputs()
    if not screens:
        return ToolResult(output="", error="couldn't read the display configuration")
    biggest = max(screens, key=lambda s: s["width"] * s["height"])
    for s in screens:
        s["largest"] = s is biggest
    return ToolResult(output=json.dumps({"count": len(screens), "screens": screens}))


async def to_largest_screen(ctx) -> ToolResult:
    ok = await _run_kwin_script(_JS_LARGEST)
    return ToolResult(output="Moved it to your largest screen.") if ok \
        else ToolResult(output="", error="couldn't move the window")


async def to_screen(ctx, screen="") -> ToolResult:
    screen = str(screen).strip()
    if not screen:
        return ToolResult(output="", error="which screen?")
    try:
        idx = int(screen)
    except ValueError:
        idx = -1
    js = _JS_TO_SCREEN.replace("%TNAME%", json.dumps(screen)).replace("%TI%", str(idx))
    ok = await _run_kwin_script(js)
    return ToolResult(output=f"Moved it to {screen}.") if ok \
        else ToolResult(output="", error="couldn't move the window")


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
                        summary="Move the active window to the next monitor (cycles)",
                        backend=_kwin("Window One Screen to the Right"),
                        examples=["move this to the next screen", "cycle it to another monitor"]),
                Command(id="window.to_largest_screen", domain="window", tier=1,
                        summary="Move the active window to the largest monitor",
                        backend=PyBackend(ref="gabagent.commands.providers.desktop:to_largest_screen"),
                        examples=["move it to the biggest screen", "put the movie on my largest monitor"]),
                Command(id="window.to_screen", domain="window", tier=1,
                        summary="Move the active window to a specific monitor by name or number",
                        backend=PyBackend(ref="gabagent.commands.providers.desktop:to_screen"),
                        params=[Slot("screen", "string", True,
                                     description="monitor name (e.g. 'DP-1') or number from window.list_screens")],
                        examples=["move it to DP-1", "put it on screen 2"]),
                Command(id="window.switch", domain="window", tier=1,
                        summary="Switch to the next window", backend=_kwin("Walk Through Windows"),
                        examples=["switch windows", "next window"]),
                Command(id="window.close", domain="window", tier=2,
                        summary="Close the active window", confirm_template="Close the active window?",
                        backend=_kwin("Window Close"), examples=["close this window", "close this"]),
            ]

        if shutil.which("kscreen-doctor"):
            cmds.append(Command(
                id="window.list_screens", domain="window", tier=1, structured=True,
                summary="List the monitors and their sizes (and which is largest)",
                backend=PyBackend(ref="gabagent.commands.providers.desktop:list_screens"),
                examples=["how many monitors do I have", "list my screens"]))

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
