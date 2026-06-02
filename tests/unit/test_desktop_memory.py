"""Desktop/window provider + voice capability-grounding and growing-memory layer."""
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
                    backend=ShellBackend(argv=["true"])))
    cat.add(Command(id="window.close", domain="window", tier=2, summary="Close the active window",
                    backend=ShellBackend(argv=["true"])))
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

def test_capability_brief_lists_real_catalog(tmp_path):
    ctx = _ctx(tmp_path, command_catalog=_media_catalog())
    brief = _capability_brief(ctx)
    assert "Do NOT deny" in brief and "Play a movie" in brief and "media" in brief


def test_capability_brief_includes_exact_ids_and_params(tmp_path):
    from gabagent.commands.model import Slot
    cat = CommandCatalog()
    cat.add(Command(id="tidal.play", domain="media", tier=1, summary="Play music on TIDAL",
                    backend=ShellBackend(argv=["true"]),
                    params=[Slot("query", "string", False), Slot("uri", "string", False)]))
    cat.add(Command(id="jellyfin.control", domain="media", tier=1, summary="Control playback",
                    backend=ShellBackend(argv=["true"]),
                    params=[Slot("action", "enum", True, enum=("pause", "resume", "stop"))]))
    brief = _capability_brief(_ctx(tmp_path, command_catalog=cat))
    # exact id + optional params marked with ?, so the model can call run_command directly
    assert "tidal.play(query?, uri?)" in brief
    # required enum slot shows its allowed values and no ?
    assert "jellyfin.control(action=pause|resume|stop)" in brief
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
