"""Part B — voice project attach: resolver security, staged cwd swap, tools, CF-truncation fix."""
from __future__ import annotations
import asyncio
import pathlib
from pathlib import Path

from gabagent.agent.context import AgentContext
from gabagent.config.models import GabAgentConfig
from gabagent.voice.session import VoiceSession
from gabagent.voice import turn as vturn
from gabagent.voice.turn import _current_focus_brief
from gabagent.tools.project_tool import (
    resolve_project, _safe_root, WorkOnProjectTool, LeaveProjectTool,
)


def _ctx(cfg, cwd, **kw):
    return AgentContext(config=cfg, client=None, rate_limiter=None, session=None,
                        session_id="t", cwd=cwd, **kw)


def _roots(tmp_path):
    root = tmp_path / "dev"
    (root / "gabagent").mkdir(parents=True)
    (root / "voice-agent").mkdir()
    (root / ".hidden").mkdir()          # dot-dir → skipped
    (root / "notes.txt").write_text("x")  # file → skipped
    cfg = GabAgentConfig()
    cfg.voice_attachable_roots = [str(root)]
    return root, cfg


# --------------------------------------------------------------------------- resolver
def test_resolve_matches_child_and_lists(tmp_path):
    root, cfg = _roots(tmp_path)
    target, available = resolve_project("gabagent", cfg)
    assert target == (root / "gabagent").resolve()
    assert available == ["gabagent", "voice-agent"]   # .hidden + notes.txt excluded


def test_resolve_empty_allowlist_is_off():
    assert resolve_project("gabagent", GabAgentConfig()) == (None, [])


def test_resolve_rejects_traversal_name(tmp_path):
    # spoken input is matched against child NAMES, never path-joined → "../etc" cannot escape.
    root, cfg = _roots(tmp_path)
    target, _ = resolve_project("../etc", cfg)
    assert target is None
    target, _ = resolve_project("/etc", cfg)
    assert target is None


def test_resolve_unknown_name(tmp_path):
    _, cfg = _roots(tmp_path)
    assert resolve_project("nonexistent", cfg)[0] is None


def test_safe_root_rejects_dangerous_roots(tmp_path):
    assert _safe_root((tmp_path / "dev").resolve()) is True
    # A normal project parent UNDER $HOME must stay safe (regression: don't reject all of ~/*).
    assert _safe_root((Path.home() / "dev").resolve()) is True
    assert _safe_root((Path.home() / "projects").resolve()) is True
    # …but $HOME itself, /, sensitive dirs, and ancestors-of-sensitive are refused.
    assert _safe_root(Path("/")) is False
    assert _safe_root(Path.home().resolve()) is False
    assert _safe_root((Path.home() / ".config").resolve()) is False
    assert _safe_root((Path.home() / ".ssh").resolve()) is False
    assert _safe_root(Path("/home")) is False   # ancestor of ~/.ssh


def test_resolve_skips_unsafe_root(tmp_path, monkeypatch):
    # if HOME itself is allow-listed, no children are attachable (root refused).
    cfg = GabAgentConfig()
    cfg.voice_attachable_roots = [str(Path.home())]
    assert resolve_project("anything", cfg) == (None, [])


# --------------------------------------------------------------------------- staged cwd swap
def test_stage_apply_attach_and_detach(tmp_path):
    root, cfg = _roots(tmp_path)
    orig = tmp_path / "service"
    orig.mkdir()
    ctx = _ctx(cfg, cwd=orig)
    vs = VoiceSession("s", ctx)
    # no pending → no change
    assert vs.apply_pending_project(ctx) is None
    assert ctx.cwd == orig
    # attach
    proj = (root / "gabagent").resolve()
    vs.stage_project(proj)
    assert vs.apply_pending_project(ctx) == proj
    assert ctx.cwd == proj
    assert vs.base_cwd == orig            # origin saved for restore
    # detach restores origin
    vs.stage_project(None)
    assert vs.apply_pending_project(ctx) is None
    assert ctx.cwd == orig
    assert vs.active_project is None


# --------------------------------------------------------------------------- tools
def test_work_on_project_requires_voice(tmp_path):
    _, cfg = _roots(tmp_path)
    ctx = _ctx(cfg, cwd=tmp_path)            # voice_session default None
    res = asyncio.run(WorkOnProjectTool().execute(ctx, name="gabagent"))
    assert res.error and "voice" in res.error.lower()


def test_work_on_project_stages(tmp_path):
    root, cfg = _roots(tmp_path)
    ctx = _ctx(cfg, cwd=tmp_path)
    vs = VoiceSession("s", ctx)
    ctx.voice_session = vs
    res = asyncio.run(WorkOnProjectTool().execute(ctx, name="gabagent"))
    assert "gabagent" in res.output.lower()
    assert vs._pending_project == (root / "gabagent").resolve()


def test_work_on_project_none_configured(tmp_path):
    ctx = _ctx(GabAgentConfig(), cwd=tmp_path)
    ctx.voice_session = VoiceSession("s", ctx)
    res = asyncio.run(WorkOnProjectTool().execute(ctx, name="x"))
    assert "no projects" in res.output.lower()


def test_leave_project(tmp_path):
    root, cfg = _roots(tmp_path)
    ctx = _ctx(cfg, cwd=tmp_path)
    vs = VoiceSession("s", ctx)
    ctx.voice_session = vs
    res = asyncio.run(LeaveProjectTool().execute(ctx))       # not attached
    assert "not working" in res.output.lower()
    vs.active_project = (root / "gabagent").resolve()
    res = asyncio.run(LeaveProjectTool().execute(ctx))       # attached → stages detach
    assert vs._pending_project is None


# --------------------------------------------------------------------------- project Current Focus brief
def test_current_focus_brief(monkeypatch, tmp_path):
    cf = "## Current Focus\n\nThe canary is PURPLE PELICAN 47.\n"
    filler = "\n".join(f"## note {i}\n\n" + "x" * 80 for i in range(30))
    full = cf + "\n" + filler
    assert len(full) > 1500                 # long file: CF must still surface (it's at the top)

    class FakeMM:
        def __init__(self, cwd):
            pass

        def load(self):
            return full

    monkeypatch.setattr("gabagent.session.memory.MemoryManager", FakeMM)
    ctx = _ctx(GabAgentConfig(), cwd=tmp_path / "gabagent")
    vs = VoiceSession("s", ctx)
    ctx.voice_session = vs

    # not attached → no CF surfaced (must NOT fire for the default cwd)
    assert _current_focus_brief(ctx) == ""

    # attached → CF surfaces, project-named, canary intact (branch-independent of TMI)
    vs.active_project = tmp_path / "gabagent"
    out = _current_focus_brief(ctx)
    assert out.startswith("Current focus on gabagent:")
    assert "PURPLE PELICAN 47" in out

    # attached but memory has no Current Focus block → empty
    class FakeMM2(FakeMM):
        def load(self):
            return "just some notes, no heading here"

    monkeypatch.setattr("gabagent.session.memory.MemoryManager", FakeMM2)
    assert _current_focus_brief(ctx) == ""


# --------------------------------------------------------------------------- meta-command fast-path
def test_meta_attach_fastpath(tmp_path):
    from gabagent.voice.commands import detect_meta_command
    root, cfg = _roots(tmp_path)
    # HIT: "work on gabagent" → attach with the resolved path
    mc = detect_meta_command("work on gabagent", cfg)
    assert mc is not None and mc.kind == "attach"
    assert mc.value == str((root / "gabagent").resolve())
    # "switch to voice-agent" (hyphenated name) also attaches
    mc = detect_meta_command("switch to voice-agent", cfg)
    assert mc is not None and mc.kind == "attach" and mc.value.endswith("voice-agent")
    # MISS (not an attachable project) → None (LLM fallback owns the messaging), NOT a false attach
    assert detect_meta_command("work on my taxes", cfg) is None
    # GREEDINESS guard: a descriptive phrase must not smuggle an embedded project name into the resolver
    assert detect_meta_command("work on the bug in gabagent", cfg) is None
    # detach phrases
    assert detect_meta_command("leave the project", cfg).value == ""
    assert detect_meta_command("stop working on this", cfg).value == ""
    # config=None → fast-path inert (every existing text-only caller unaffected)
    assert detect_meta_command("work on gabagent", None) is None
    # RESERVED alias not shadowed: a brain switch still wins even with config present
    assert detect_meta_command("switch to local", cfg).kind == "brain"


def test_stage_helpers(tmp_path):
    from gabagent.tools.project_tool import stage_attach, stage_detach
    root, cfg = _roots(tmp_path)
    ctx = _ctx(cfg, cwd=tmp_path)
    vs = VoiceSession("s", ctx)
    assert "not working on a project" in stage_detach(vs).lower()   # nothing attached
    msg = stage_attach(vs, root / "gabagent")
    assert "gabagent" in msg.lower()
    assert vs._pending_project == (root / "gabagent")
    vs.active_project = root / "gabagent"
    assert "stopped working" in stage_detach(vs).lower()
    assert vs._pending_project is None
