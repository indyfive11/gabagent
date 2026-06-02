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
    assert "tidal.play(query?, uri?)" in brief                      # optional params marked ?
    assert "jellyfin.control(action=pause|resume|stop)" in brief    # required enum, no ?
    assert "run_command(command_id, args)" in brief


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


async def test_to_screen_json_encodes_target_no_injection(monkeypatch):
    from gabagent.commands.providers import desktop as d
    captured = {}
    async def fake_script(js): captured["js"] = js; return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    res = await d.to_screen(None, screen='DP-1"; evil()//')
    assert res.success
    assert json.dumps('DP-1"; evil()//') in captured["js"]   # value is a JS string literal, can't break out
    assert "%TNAME%" not in captured["js"] and "%TI%" not in captured["js"]


async def test_to_largest_screen_runs_script(monkeypatch):
    from gabagent.commands.providers import desktop as d
    async def fake_script(js): return True
    monkeypatch.setattr(d, "_run_kwin_script", fake_script)
    assert (await d.to_largest_screen(None)).success
