"""Desktop & window control for KDE Plasma (Wayland-friendly).

Window operations go through KWin's global shortcuts over DBus (`qdbus6 … invokeShortcut`),
which is the reliable Wayland path — no X11-only tools (wmctrl/xdotool) needed. Screenshots use
spectacle, quitting an app uses kquitapp6, and a couple of keyboard-driven actions use wtype.
Each command is published only if its specific binary is present, so the catalog adapts to the host.

Most window ops target the active window (KWin shortcuts act on it); there is no arbitrary keystroke
injector. Targeting a specific window by name uses a one-shot KWin script (workspace.windowList() +
window.closeWindow()) — see window.close_named.
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

# Move the ACTIVE window to a named output (%TNAME%). Used only when we have no better target —
# while the user is talking to the voice agent the active window is usually NOT the one they mean.
_JS_ACTIVE_TO_SCREEN = (
    "(function(){var w=workspace.activeWindow;if(!w)return;var tn=%TNAME%;"
    "var s=workspace.screens;for(var i=0;i<s.length;i++){"
    "if(s[i].name===tn){workspace.sendClientToScreen(w,s[i]);return;}}})();"
)
# Move the window whose caption OR app class contains %NAME% (lowercased substring) to output
# %TNAME%. This targets the *movie* window by title even when it isn't focused — the activeWindow
# approach silently moved the wrong window (the focused terminal/voice UI) and still reported success.
_JS_MOVE_NAMED = (
    "(function(){var n=%NAME%;var tn=%TNAME%;var ws=workspace.windowList();var w=null;"
    "for(var i=0;i<ws.length;i++){var c=(ws[i].caption||'').toLowerCase();"
    "var k=(ws[i].resourceClass||'').toLowerCase();"
    "if(c.indexOf(n)>=0||k.indexOf(n)>=0){w=ws[i];break;}}"
    "if(!w)return;var s=workspace.screens;for(var j=0;j<s.length;j++){"
    "if(s[j].name===tn){workspace.sendClientToScreen(w,s[j]);return;}}})();"
)
# Fullscreen the first window whose caption OR app class contains %NAME% (lowercased substring).
_JS_FULLSCREEN_NAMED = (
    "(function(){var n=%NAME%;var ws=workspace.windowList();for(var i=0;i<ws.length;i++){"
    "var c=(ws[i].caption||'').toLowerCase();var k=(ws[i].resourceClass||'').toLowerCase();"
    "if(c.indexOf(n)>=0||k.indexOf(n)>=0){ws[i].fullScreen=true;return;}}})();"
)
# Drop that window OUT of KWin fullscreen (and raise+activate it so a synthetic key can reach it).
_JS_UNFULLSCREEN_NAMED = (
    "(function(){var n=%NAME%;var ws=workspace.windowList();for(var i=0;i<ws.length;i++){"
    "var c=(ws[i].caption||'').toLowerCase();var k=(ws[i].resourceClass||'').toLowerCase();"
    "if(c.indexOf(n)>=0||k.indexOf(n)>=0){ws[i].fullScreen=false;workspace.activeWindow=ws[i];return;}}})();"
)
# Close the first window whose caption OR app class contains %NAME% (lowercased substring). Plasma 6
# KWin scripting: workspace.windowList() + window.closeWindow() (verified on the host).
_JS_CLOSE_NAMED = (
    "(function(){var n=%NAME%;var ws=workspace.windowList();for(var i=0;i<ws.length;i++){"
    "var w=ws[i];var c=(w.caption||'').toLowerCase();var k=(w.resourceClass||'').toLowerCase();"
    "if(c.indexOf(n)>=0||k.indexOf(n)>=0){w.closeWindow();return;}}})();"
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


def _screen_aliases(ctx) -> dict:
    """User-configured friendly monitor name → connector (e.g. {'hisense': 'DP-1'}); empty if none."""
    try:
        return dict(ctx.config.desktop.screen_aliases or {})
    except Exception:
        return {}


def _resolve_screen(query: str, screens: list[dict], aliases: dict | None = None) -> dict | None:
    """Map a spoken screen reference to a real output, or None if it doesn't match any.

    The KWin output name is the connector (DP-1, HDMI-A-1) — the display's make/model (e.g.
    "Hisense") is NOT exposed by kscreen-doctor or sysfs here, so a request we can't resolve must
    fail honestly rather than silently no-op while reporting success. A user can bridge a make/model
    to a connector via config `desktop.screen_aliases` (e.g. {"hisense": "DP-1"})."""
    q = query.strip().lower()
    if not q:
        return None
    for name, connector in (aliases or {}).items():   # friendly alias (substring) → connector
        if name.strip().lower() in q:
            q = str(connector).strip().lower()
            break
    if any(w in q for w in ("large", "big", "main", "primary")):
        return max(screens, key=lambda s: s["width"] * s["height"])
    try:                                   # 1-based index ("screen 2")
        i = int(q)
        if 1 <= i <= len(screens):
            return screens[i - 1]
    except ValueError:
        pass
    for s in screens:                      # exact connector name
        if s["name"].lower() == q:
            return s
    for s in screens:                      # connector substring ("hdmi" → HDMI-A-1)
        if q in s["name"].lower():
            return s
    return None


def _clean_title(t: str) -> str:
    """Drop year/quality tags so the title matches the browser window caption:
    '12 Angry Men (1957)' / 'The Matrix [1080p]' → 'the matrix'."""
    return re.split(r"[\(\[]", t or "", maxsplit=1)[0].strip().lower()


async def _movie_window_hint(ctx) -> str:
    """Caption hint for the movie window, so a move/fullscreen targets it instead of whatever is
    focused. Works for a page WE own (live title) AND for a movie played into an UNOWNED window (the
    title stored at play time) — that unowned-Chrome case was moving the wrong window before. Empty if
    no movie is in play."""
    if ctx is None:
        return ""
    page = getattr(ctx, "jellyfin_playing_page", None)
    if page is not None:
        try:
            if not page.is_closed():
                return _clean_title(await page.title() or "")
        except Exception:
            pass
    return _clean_title(getattr(ctx, "jellyfin_playing_title", None) or "")


async def _move_to_target(ctx, target: dict) -> ToolResult:
    """Move the movie window (by title) if one is open, else the active window, to `target` output.
    Phrasing is honest about which it moved and that we can't confirm KWin actually relocated it."""
    hint = await _movie_window_hint(ctx)
    if hint:
        js = _JS_MOVE_NAMED.replace("%NAME%", json.dumps(hint.lower())) \
                           .replace("%TNAME%", json.dumps(target["name"]))
        what = "the movie"
    else:
        js = _JS_ACTIVE_TO_SCREEN.replace("%TNAME%", json.dumps(target["name"]))
        what = "this window"
    ok = await _run_kwin_script(js)
    return ToolResult(output=f"Sent {what} to {target['name']}.") if ok \
        else ToolResult(output="", error="couldn't move the window")


async def to_largest_screen(ctx) -> ToolResult:
    screens = await _kscreen_outputs()
    if not screens:
        return ToolResult(output="", error="couldn't read the display configuration")
    return await _move_to_target(ctx, max(screens, key=lambda s: s["width"] * s["height"]))


async def to_screen(ctx, screen="") -> ToolResult:
    screen = str(screen).strip()
    if not screen:
        return ToolResult(output="", error="which screen?")
    screens = await _kscreen_outputs()
    if not screens:
        return ToolResult(output="", error="couldn't read the display configuration")
    target = _resolve_screen(screen, screens, _screen_aliases(ctx))
    if target is None:
        # Honest failure: don't claim a move that didn't happen (the "Hisense" false-success bug).
        names = ", ".join(s["name"] for s in screens)
        biggest = max(screens, key=lambda s: s["width"] * s["height"])["name"]
        return ToolResult(output="", error=(
            f"I don't see a screen called '{screen}'. I have {names} — the largest is {biggest}. "
            "Tell me one of those, or say 'the largest screen'."))
    return await _move_to_target(ctx, target)


async def to_movie_screen(ctx) -> str:
    """Move the movie window to the user's configured movie screen (`desktop.movie_screen`, default
    DP-1) before fullscreening it. Best-effort: returns the output name it targeted, or "" when there's
    no movie window, no display info, the setting is blank, or the configured screen isn't currently
    connected — so auto-fullscreen-on-play degrades quietly instead of erroring the play.

    Emits a `movie_screen` debug event with a `reason` at every exit so a live verify run can see WHY a
    move did or didn't happen (the prior version was invisible in the brain log — we could only infer it
    from what the bot said)."""
    from gabagent.voice.debuglog import dlog
    if ctx is None:
        return ""
    want = (getattr(getattr(ctx.config, "desktop", None), "movie_screen", "") or "").strip()
    if not want:
        dlog(ctx, "movie_screen", reason="blank_setting", moved="")
        return ""
    screens = await _kscreen_outputs()
    if not screens:
        dlog(ctx, "movie_screen", reason="no_display_info", want=want, moved="")
        return ""
    target = _resolve_screen(want, screens, _screen_aliases(ctx))
    if target is None:
        dlog(ctx, "movie_screen", reason="screen_not_connected", want=want,
             have=[s["name"] for s in screens], moved="")
        return ""
    hint = await _movie_window_hint(ctx)
    if not hint:
        dlog(ctx, "movie_screen", reason="no_movie_window", want=want, target=target["name"], moved="")
        return ""
    js = _JS_MOVE_NAMED.replace("%NAME%", json.dumps(hint.lower())) \
                       .replace("%TNAME%", json.dumps(target["name"]))
    ok = await _run_kwin_script(js)
    dlog(ctx, "movie_screen", reason="moved" if ok else "kwin_move_failed",
         want=want, target=target["name"], window=hint, moved=target["name"] if ok else "")
    return target["name"] if ok else ""


async def fullscreen(ctx) -> ToolResult:
    """Fullscreen the MOVIE window by caption when a movie is playing (even in an unowned Chrome) —
    otherwise the active window via the global shortcut. The old active-window-only path fullscreened
    whatever was focused, which was the wrong window when the movie wasn't focused."""
    hint = await _movie_window_hint(ctx)
    if hint:
        ok = await _run_kwin_script(_JS_FULLSCREEN_NAMED.replace("%NAME%", json.dumps(hint)))
        return ToolResult(output="Set the movie to full screen.") if ok \
            else ToolResult(output="", error="couldn't set full screen")
    rc, _ = await _run([*_KGA, "Window Fullscreen"])
    return ToolResult(output="Set it to full screen.") if rc == 0 \
        else ToolResult(output="", error="couldn't set full screen")


async def exit_movie_fullscreen(ctx) -> bool:
    """Drop the movie window out of KWin fullscreen (caption-matched) and activate it. True if the
    script ran. The Jellyfin player's OWN (HTML5) fullscreen is handled separately on the page."""
    hint = await _movie_window_hint(ctx)
    if not hint:
        return False
    return await _run_kwin_script(_JS_UNFULLSCREEN_NAMED.replace("%NAME%", json.dumps(hint)))


async def close_named(ctx, name="") -> ToolResult:
    name = str(name).strip()
    if not name:
        return ToolResult(output="", error="which window?")
    # The KWin script only reports that it ran, not whether a window matched — so phrase the result
    # conditionally rather than claiming a window was definitely closed.
    js = _JS_CLOSE_NAMED.replace("%NAME%", json.dumps(name.lower()))
    ok = await _run_kwin_script(js)
    return ToolResult(output=f"If a {name} window was open, I've closed it.") if ok \
        else ToolResult(output="", error="couldn't run the window command")


class DesktopProvider:
    id = "desktop"

    async def detect(self, ctx: AgentContext) -> bool:
        return any(shutil.which(b) for b in ("qdbus6", "spectacle", "kquitapp6", "wtype"))

    def commands(self, ctx: AgentContext) -> list[Command]:
        cmds: list[Command] = []

        if shutil.which("qdbus6"):
            cmds += [
                Command(id="window.maximize", domain="window", tier=1, featured=True,
                        summary="Maximize the active window", backend=_kwin("Window Maximize"),
                        examples=["maximize this", "make this window full size"]),
                Command(id="window.minimize", domain="window", tier=1,
                        summary="Minimize the active window", backend=_kwin("Window Minimize"),
                        examples=["minimize this", "hide this window"]),
                Command(id="window.fullscreen", domain="window", tier=1,
                        summary="Make the movie (or the active window) fullscreen",
                        backend=PyBackend(ref="gabagent.commands.providers.desktop:fullscreen"),
                        examples=["go fullscreen", "fullscreen the movie"]),
                Command(id="window.center", domain="window", tier=1,
                        summary="Center the active window", backend=_kwin("Window Move Center")),
                Command(id="window.to_other_screen", domain="window", tier=1,
                        summary="Move the active window to the next monitor (cycles)",
                        backend=_kwin("Window One Screen to the Right"),
                        examples=["move this to the next screen", "cycle it to another monitor"]),
                Command(id="window.to_largest_screen", domain="window", tier=1, featured=True,
                        summary="Move the active window to the largest monitor",
                        backend=PyBackend(ref="gabagent.commands.providers.desktop:to_largest_screen"),
                        examples=["move it to the biggest screen", "put the movie on my largest monitor"]),
                Command(id="window.to_screen", domain="window", tier=1,
                        summary="Move the active window to a specific monitor by connector name, "
                                "number, or 'largest'",
                        backend=PyBackend(ref="gabagent.commands.providers.desktop:to_screen"),
                        params=[Slot("screen", "string", True,
                                     description="connector name (e.g. 'DP-1', 'HDMI-A-1'), a 1-based "
                                     "number from window.list_screens, or 'largest'/'primary'. A "
                                     "display make/model (e.g. 'Hisense') is NOT a valid name — call "
                                     "window.list_screens first if unsure")],
                        examples=["move it to DP-1", "put it on screen 2", "move it to the largest screen"]),
                Command(id="window.switch", domain="window", tier=1,
                        summary="Switch to the next window", backend=_kwin("Walk Through Windows"),
                        examples=["switch windows", "next window"]),
                Command(id="window.close", domain="window", tier=2,
                        summary="Close the active window", confirm_template="Close the active window?",
                        backend=_kwin("Window Close"), examples=["close this window", "close this"]),
                Command(id="window.close_named", domain="window", tier=2,
                        summary="Close a specific window by name (its title or app), not just the active one",
                        confirm_template="Close the {name} window?",
                        backend=PyBackend(ref="gabagent.commands.providers.desktop:close_named"),
                        params=[Slot("name", "string", True,
                                     description="part of the window title or app, e.g. 'dolphin', 'vivaldi', 'jellyfin'")],
                        examples=["close the Dolphin window", "close the Vivaldi window",
                                  "close the window called Steam"]),
            ]

        if shutil.which("kscreen-doctor"):
            cmds.append(Command(
                id="window.list_screens", domain="window", tier=1, structured=True, featured=True,
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
