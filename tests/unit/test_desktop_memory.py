"""Desktop/window provider + voice capability-grounding and growing-memory layer."""
import json
import types
from pathlib import Path
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.catalog import CommandCatalog
from gabagent.commands.model import Command, ShellBackend
from gabagent.commands.providers.desktop import DesktopProvider
from gabagent.session.memory import MemoryManager
from gabagent.voice import commands as vc
from gabagent.voice.turn import _voice_system, _capability_brief


async def _aw(value):
    """Wrap a plain value as an awaitable, for monkeypatching async helpers."""
    return value


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _ctx(tmp_path, **kw):
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(
        config=cfg, cwd=tmp_path, local_mode=False, local_context_summary=None,
        command_catalog=None, voice_session=None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _media_catalog():
    cat = CommandCatalog()
    cat.add(Command(id="jellyfin.play", domain="media", tier=2, summary="Play a movie",
                    featured=True, backend=ShellBackend(argv=["true"])))   # hot-set
    cat.add(Command(id="window.close", domain="window", tier=2, summary="Close the active window",
                    backend=ShellBackend(argv=["true"])))                  # long tail (index only)
    return cat


# -- desktop / window provider --------------------------------------------

def test_desktop_provider_publishes_window_ops(monkeypatch):
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which",
                        lambda b: f"/usr/bin/{b}")
    cmds = {c.id: c for c in DesktopProvider().commands(ctx=None)}
    assert cmds["window.maximize"].tier == 1
    assert cmds["window.close"].tier == 2 and cmds["window.close"].confirm_template
    assert cmds["desktop.quit_app"].tier == 2
    assert any(s.name == "app" and s.required for s in cmds["desktop.quit_app"].params)
    # window ops invoke KWin global shortcuts (Wayland-safe), never wmctrl/xdotool.
    assert cmds["window.maximize"].backend.argv[:2] == ["qdbus6", "org.kde.kglobalaccel"]


def test_desktop_provider_adapts_to_missing_binaries(monkeypatch):
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which",
                        lambda b: f"/usr/bin/{b}" if b == "spectacle" else None)
    cmds = {c.id for c in DesktopProvider().commands(ctx=None)}
    assert cmds == {"desktop.screenshot"}          # only spectacle present → only screenshot


async def test_desktop_detect_false_when_nothing(monkeypatch):
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", lambda b: None)
    assert await DesktopProvider().detect(ctx=None) is False


async def test_close_named_builds_injection_safe_kwin_js(monkeypatch):
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_run(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_run)
    r = await d.close_named(None, name='Vivaldi "X"')
    assert r.success
    assert '"vivaldi \\"x\\""' in captured["js"]            # lowercased + JSON-encoded (quotes escaped)
    assert "windowList()" in captured["js"] and "closeWindow()" in captured["js"]


async def test_close_named_requires_a_name():
    from gabagent.commands.providers import desktop as d
    r = await d.close_named(None, name="  ")
    assert not r.success and "which window" in r.error.lower()


# -- #1 capability grounding ----------------------------------------------

def test_capability_brief_is_a_domain_index_plus_hot_set(tmp_path):
    ctx = _ctx(tmp_path, command_catalog=_media_catalog())
    brief = _capability_brief(ctx)
    assert "NOT deny" in brief                          # anti-denial instruction kept
    assert "media (1)" in brief and "window (1)" in brief  # domain index with counts
    assert "Play a movie" in brief                       # featured → in the hot set
    # the long tail is NOT dumped — only reachable via list_capabilities
    assert "Close the active window" not in brief
    assert "list_capabilities" in brief                  # told to look up the rest


def test_hot_set_renders_exact_ids_and_params(tmp_path):
    from gabagent.commands.model import Slot
    cat = CommandCatalog()
    cat.add(Command(id="tidal.play", domain="media", tier=1, summary="Play music on TIDAL",
                    featured=True, backend=ShellBackend(argv=["true"]),
                    params=[Slot("query", "string", False), Slot("uri", "string", False)]))
    cat.add(Command(id="jellyfin.control", domain="media", tier=1, summary="Control playback",
                    featured=True, backend=ShellBackend(argv=["true"]),
                    params=[Slot("action", "enum", True, enum=("pause", "resume", "stop"))]))
    brief = _capability_brief(_ctx(tmp_path, command_catalog=cat))
    # Ids are rendered standalone with args LABELED — never as `id(params)`, which the model misreads
    # as a callable function and tries to invoke directly.
    assert "tidal.play — Play music on TIDAL" in brief
    assert "args: query?, uri?" in brief                            # optional params marked ?
    assert "jellyfin.control — Control playback" in brief
    assert "args: action=pause|resume|stop" in brief               # required enum, no ?
    assert "tidal.play(" not in brief and "jellyfin.control(" not in brief   # NOT function signatures
    assert "run_command" in brief and "NOT callable functions" in brief      # framed as run_command args


def test_voice_system_injects_caps_and_memory(home):
    proj = home / "proj"; proj.mkdir()
    MemoryManager(proj).append("The user prefers dark mode.")
    ctx = _ctx(proj, command_catalog=_media_catalog())
    s = _voice_system(ctx)
    assert "Play a movie" in s                       # capabilities grounded
    assert "dark mode" in s                          # memory carried in


def test_caps_meta_command():
    m = vc.detect_meta_command("what can you do")
    assert m and m.kind == "query" and m.value == "caps"
    assert vc.detect_meta_command("what are you able to do").value == "caps"


def test_capabilities_brief_spoken(tmp_path):
    ctx = _ctx(tmp_path, command_catalog=_media_catalog())
    spoken = vc.capabilities_brief(ctx)
    assert "movies" in spoken and "windows" in spoken


# -- #5 growing memory -----------------------------------------------------

def test_memory_pop_last(home):
    proj = home / "proj"; proj.mkdir()
    mgr = MemoryManager(proj)
    mgr.append("first fact")
    mgr.append("second fact")
    assert mgr.pop_last() is True
    text = mgr.load()
    assert "first fact" in text and "second fact" not in text
    assert mgr.pop_last() is True
    assert mgr.pop_last() is False                    # empty now


def test_memory_meta_commands():
    assert vc.detect_meta_command("what do you remember").value == "memory"
    assert vc.detect_meta_command("forget that").kind == "forget"
    assert vc.detect_meta_command("forget that").value == "last"
    assert vc.detect_meta_command("forget everything").value == "all"
    # casual speech must NOT trigger a wipe
    assert vc.detect_meta_command("clear the screen") is None
    assert vc.detect_meta_command("what does this do") is None


def test_forget_and_memory_summary(home):
    proj = home / "proj"; proj.mkdir()
    ctx = _ctx(proj)
    assert "haven't saved" in vc._memory_summary(ctx)
    MemoryManager(proj).append("Buy milk")
    assert "Buy milk" in vc._memory_summary(ctx)
    assert "forgotten the last" in vc.forget(ctx, "last")
    assert "haven't saved" in vc._memory_summary(ctx)
    MemoryManager(proj).append("note a")
    assert "cleared everything" in vc.forget(ctx, "all")
    assert "haven't saved" in vc._memory_summary(ctx)


# -- multi-monitor targeting (KWin + kscreen-doctor) -----------------------

_KSCREEN = (
    "Output: 1 DP-1\n\tenabled\n\tpriority 1\n\tGeometry: 768,0 3072x1728\n"
    "Output: 2 DP-2\n\tenabled\n\tpriority 3\n\tGeometry: 0,1728 1920x1080\n"
    "Output: 3 HDMI-A-1\n\tenabled\n\tpriority 4\n\tGeometry: 3840,1728 1920x1080\n"
)


async def test_list_screens_parses_names_sizes_largest(monkeypatch):
    from gabagent.commands.providers import desktop as d
    async def fake_run(argv, timeout=5.0): return (0, _KSCREEN)
    monkeypatch.setattr(d, "_run", fake_run)
    data = json.loads((await d.list_screens(None)).output)
    assert data["count"] == 3
    dp1 = next(s for s in data["screens"] if s["name"] == "DP-1")
    assert dp1["primary"] and dp1["largest"] and dp1["width"] == 3072
    assert sum(s["largest"] for s in data["screens"]) == 1   # exactly one largest


async def test_run_kwin_script_load_start_unload(monkeypatch):
    from gabagent.commands.providers import desktop as d
    calls = []
    async def fake_run(argv, timeout=5.0):
        calls.append(argv); return (0, "1")
    monkeypatch.setattr(d, "_run", fake_run)
    assert await d._run_kwin_script("/*js*/") is True
    methods = [next(t for t in a if t.startswith("org.kde.kwin.Scripting.")) for a in calls]
    assert methods == ["org.kde.kwin.Scripting.unloadScript", "org.kde.kwin.Scripting.loadScript",
                       "org.kde.kwin.Scripting.start", "org.kde.kwin.Scripting.unloadScript"]


_FAKE_SCREENS = [
    {"name": "DP-1", "width": 3072, "height": 1728, "primary": True},
    {"name": "DP-2", "width": 1920, "height": 1080, "primary": False},
    {"name": "HDMI-A-1", "width": 1920, "height": 1080, "primary": False},
]


async def test_to_screen_resolves_and_json_encodes_connector(monkeypatch):
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    monkeypatch.setattr(d, "_kscreen_outputs", lambda: _aw(_FAKE_SCREENS))
    res = await d.to_screen(None, screen="hdmi")          # substring → HDMI-A-1
    assert res.success and "HDMI-A-1" in res.output
    assert json.dumps("HDMI-A-1") in captured["js"]        # only the resolved connector reaches the JS
    assert "%TNAME%" not in captured["js"] and "%TI%" not in captured["js"]


async def test_to_screen_unknown_name_fails_honestly(monkeypatch):
    """A display make/model (e.g. 'Hisense') resolves to no connector → honest error, NO false success
    and the KWin script is never run (the bug: it claimed 'Moved it' after a silent no-op)."""
    from gabagent.commands.providers import desktop as d
    ran = []
    async def fake_script(js): ran.append(js); return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    monkeypatch.setattr(d, "_kscreen_outputs", lambda: _aw(_FAKE_SCREENS))
    res = await d.to_screen(None, screen="Hisense")
    assert not res.success
    assert "Hisense" in res.error and "DP-1" in res.error   # names the real screens
    assert ran == []                                        # never pretended to move


def test_resolve_screen_largest_index_name(monkeypatch):
    from gabagent.commands.providers.desktop import _resolve_screen
    assert _resolve_screen("largest", _FAKE_SCREENS)["name"] == "DP-1"
    assert _resolve_screen("the biggest screen", _FAKE_SCREENS)["name"] == "DP-1"
    assert _resolve_screen("2", _FAKE_SCREENS)["name"] == "DP-2"      # 1-based index
    assert _resolve_screen("DP-2", _FAKE_SCREENS)["name"] == "DP-2"   # exact connector
    assert _resolve_screen("hdmi", _FAKE_SCREENS)["name"] == "HDMI-A-1"  # substring
    assert _resolve_screen("Hisense", _FAKE_SCREENS) is None          # make/model → unresolvable


def test_resolve_screen_alias(monkeypatch):
    from gabagent.commands.providers.desktop import _resolve_screen
    aliases = {"hisense": "DP-1"}
    # The make/model now resolves via the configured alias, even embedded in a phrase.
    assert _resolve_screen("Hisense", _FAKE_SCREENS, aliases)["name"] == "DP-1"
    assert _resolve_screen("move it to the hisense monitor", _FAKE_SCREENS, aliases)["name"] == "DP-1"
    assert _resolve_screen("nope", _FAKE_SCREENS, aliases) is None


class _FakePage:
    def __init__(self, title): self._title = title; self._closed = False
    def is_closed(self): return self._closed
    async def title(self): return self._title


async def test_to_screen_targets_movie_window_by_title(monkeypatch):
    """When a movie WE launched is open, the move targets that window by caption via windowList()
    (not activeWindow, which is usually the focused terminal/voice UI)."""
    import types
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    monkeypatch.setattr(d, "_kscreen_outputs", lambda: _aw(_FAKE_SCREENS))
    ctx = types.SimpleNamespace(jellyfin_playing_page=_FakePage("What Dreams May Come"),
                                config=types.SimpleNamespace(desktop=types.SimpleNamespace(
                                    screen_aliases={"hisense": "DP-1"})))
    res = await d.to_screen(ctx, screen="hisense")            # alias → DP-1
    assert res.success and "movie" in res.output and "DP-1" in res.output
    assert "windowList" in captured["js"]                     # by-title path, not activeWindow
    assert json.dumps("what dreams may come") in captured["js"]   # caption hint
    assert json.dumps("DP-1") in captured["js"]


async def test_to_screen_falls_back_to_active_window(monkeypatch):
    """No movie open → move the active window (still by resolved connector name)."""
    import types
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    monkeypatch.setattr(d, "_kscreen_outputs", lambda: _aw(_FAKE_SCREENS))
    ctx = types.SimpleNamespace(jellyfin_playing_page=None,
                                config=types.SimpleNamespace(desktop=types.SimpleNamespace(screen_aliases={})))
    res = await d.to_screen(ctx, screen="DP-2")
    assert res.success and "this window" in res.output
    assert "activeWindow" in captured["js"] and "windowList" not in captured["js"]


async def test_to_largest_screen_moves_to_biggest(monkeypatch):
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    monkeypatch.setattr(d, "_kscreen_outputs", lambda: _aw(_FAKE_SCREENS))
    res = await d.to_largest_screen(None)
    assert res.success and "DP-1" in res.output              # DP-1 is the largest of _FAKE_SCREENS


def test_catalog_resolves_window_move_alias():
    """The model keeps inventing `window.move_window`; the catalog aliases it to `window.to_screen`
    so the guess just works instead of an Unknown-command turn."""
    from gabagent.commands.catalog import CommandCatalog
    from gabagent.commands.model import Command, ShellBackend
    cat = CommandCatalog()
    cmd = Command(id="window.to_screen", domain="window", summary="move it", tier=1,
                  backend=ShellBackend(argv=["true"]))
    cat.add(cmd)
    assert cat.get("window.move_window") is cmd     # alias resolves
    assert cat.get("window.to_screen") is cmd       # real id still works
    assert cat.get("window.bogus") is None          # unknown still None


def test_catalog_fuzzy_window_move_alias():
    """The model riffs on window-move names; the whole family resolves to window.to_screen."""
    from gabagent.commands.catalog import CommandCatalog
    from gabagent.commands.model import Command, ShellBackend
    cat = CommandCatalog()
    cmd = Command(id="window.to_screen", domain="window", summary="move it", tier=1,
                  backend=ShellBackend(argv=["true"]))
    cat.add(cmd)
    for invented in ("window.move_window", "window.move_window_to_screen",
                     "window.move_to_monitor", "window.move", "desktop.move_window_to_display"):
        assert cat.get(invented) is cmd, invented
    assert cat.get("window.maximize") is None        # unrelated unknown id not hijacked


def test_clean_title_strips_year_and_quality_tags():
    from gabagent.commands.providers.desktop import _clean_title
    assert _clean_title("12 Angry Men (1957)") == "12 angry men"
    assert _clean_title("The Matrix [1080p] [YTS]") == "the matrix"
    assert _clean_title("Inception") == "inception"


async def test_movie_hint_uses_stored_title_for_unowned_window():
    import types
    from gabagent.commands.providers import desktop as d
    ctx = types.SimpleNamespace(jellyfin_playing_page=None, jellyfin_playing_title="12 Angry Men (1957)")
    assert await d._movie_window_hint(ctx) == "12 angry men"   # works with NO owned page (REST/unowned)


async def test_to_screen_targets_unowned_movie_by_stored_title(monkeypatch):
    import types
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    monkeypatch.setattr(d, "_kscreen_outputs", lambda: _aw(_FAKE_SCREENS))
    ctx = types.SimpleNamespace(jellyfin_playing_page=None, jellyfin_playing_title="12 Angry Men",
                                config=types.SimpleNamespace(desktop=types.SimpleNamespace(screen_aliases={})))
    res = await d.to_screen(ctx, screen="DP-1")
    assert res.success and "movie" in res.output
    assert "windowList" in captured["js"] and json.dumps("12 angry men") in captured["js"]


async def test_fullscreen_targets_movie_window_when_playing(monkeypatch):
    import types
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    ctx = types.SimpleNamespace(jellyfin_playing_page=None, jellyfin_playing_title="12 Angry Men")
    res = await d.fullscreen(ctx)
    assert res.success and "movie" in res.output
    assert "fullScreen=true" in captured["js"] and json.dumps("12 angry men") in captured["js"]


async def test_fullscreen_falls_back_to_shortcut_without_movie(monkeypatch):
    import types
    from gabagent.commands.providers import desktop as d
    ran = []
    async def fake_run(argv, timeout=5.0): ran.append(argv); return (0, "")
    monkeypatch.setattr(d, "_run", fake_run)
    ctx = types.SimpleNamespace(jellyfin_playing_page=None, jellyfin_playing_title=None)
    res = await d.fullscreen(ctx)
    assert res.success and any("Window Fullscreen" in a for a in ran[0])   # global shortcut path
