import types
import pytest

from gabagent.voice import launcher


class FakeProc:
    def __init__(self, poll_value=None):
        self._poll = poll_value
        self.returncode = poll_value
        self.terminated = False

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminated = True


def _ctx(tmp_path):
    return types.SimpleNamespace(cwd=tmp_path, session_id="sess-123", voice_process=None)


def _seq_health(values):
    """A fake brain_health that returns the given values in order (last repeats)."""
    calls = {"i": 0}

    async def health(base_url, timeout=2.0):
        i = min(calls["i"], len(values) - 1)
        calls["i"] += 1
        return values[i]

    return health


async def test_attaches_when_already_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "brain_health", _seq_health([True]))

    def _no_spawn(*a, **k):
        raise AssertionError("should not spawn when a brain is already up")

    monkeypatch.setattr(launcher.subprocess, "Popen", _no_spawn)
    ctx = _ctx(tmp_path)
    running, spawned, msg = await launcher.start_brain(ctx, 8765)
    assert running is True and spawned is False
    assert ctx.voice_process is None  # we don't own it


async def test_spawns_when_down(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher, "brain_health", _seq_health([False, True]))
    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc(poll_value=None)

    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    ctx = _ctx(tmp_path)
    running, spawned, msg = await launcher.start_brain(ctx, 8770)
    assert running is True and spawned is True
    assert isinstance(ctx.voice_process, FakeProc)
    # spawned with the right flags incl. --resume for continuity
    assert "--voice-serve" in captured["cmd"]
    assert "--port" in captured["cmd"] and "8770" in captured["cmd"]
    assert "--resume" in captured["cmd"] and "sess-123" in captured["cmd"]


async def test_reports_early_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher, "brain_health", _seq_health([False]))  # never comes up
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **k: FakeProc(poll_value=1))
    ctx = _ctx(tmp_path)
    running, spawned, msg = await launcher.start_brain(ctx, 8771)
    assert running is False and spawned is True
    assert "exited" in msg


def test_stop_brain_terminates(tmp_path):
    ctx = _ctx(tmp_path)
    proc = FakeProc()
    ctx.voice_process = proc
    launcher.stop_brain(ctx)
    assert proc.terminated is True
    assert ctx.voice_process is None


def test_stop_brain_noop_when_none(tmp_path):
    ctx = _ctx(tmp_path)
    launcher.stop_brain(ctx)  # must not raise
    assert ctx.voice_process is None


async def test_brain_health_false_on_closed_port():
    assert await launcher.brain_health("http://127.0.0.1:1", timeout=0.2) is False


# -- front-end (voice-agent) launcher ---------------------------------------

def _fe_ctx(tmp_path, voice_agent_cmd=None):
    cfg = types.SimpleNamespace(voice_agent_cmd=voice_agent_cmd or [])
    return types.SimpleNamespace(cwd=tmp_path, config=cfg, voice_frontend_process=None)


def test_resolve_frontend_prefers_config_override(tmp_path):
    ctx = _fe_ctx(tmp_path, voice_agent_cmd=["/custom/run.sh", "gab"])
    assert launcher._resolve_frontend_cmd(ctx) == ["/custom/run.sh", "gab"]


def test_resolve_frontend_uses_path_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: "/usr/bin/voice-agent")
    ctx = _fe_ctx(tmp_path)
    assert launcher._resolve_frontend_cmd(ctx) == ["/usr/bin/voice-agent", "gab"]


async def test_start_frontend_spawns_with_gab_port(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda n: "/usr/bin/voice-agent")
    async def _fast_sleep(_):
        return None
    monkeypatch.setattr(launcher.asyncio, "sleep", _fast_sleep)
    captured = {}

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return FakeProc(poll_value=None)

    monkeypatch.setattr(launcher.subprocess, "Popen", _fake_popen)
    ctx = _fe_ctx(tmp_path)
    ok, msg = await launcher.start_frontend(ctx, 8780)
    assert ok is True
    assert captured["cmd"] == ["/usr/bin/voice-agent", "gab"]
    assert captured["env"]["GAB_PORT"] == "8780"
    assert captured["env"]["BRAIN"] == "gabagent"
    assert isinstance(ctx.voice_frontend_process, FakeProc)


async def test_start_frontend_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda n: None)
    # Point Path.home() at an empty tmp dir so ~/dev/voice-agent/run.sh doesn't exist.
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: tmp_path))
    ctx = _fe_ctx(tmp_path)
    ok, msg = await launcher.start_frontend(ctx, 8781)
    assert ok is False and "not found" in msg


async def test_start_frontend_reports_early_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(launcher.shutil, "which", lambda n: "/usr/bin/voice-agent")
    async def _fast_sleep(_):
        return None
    monkeypatch.setattr(launcher.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **k: FakeProc(poll_value=2))
    ctx = _fe_ctx(tmp_path)
    ok, msg = await launcher.start_frontend(ctx, 8782)
    assert ok is False and "exited" in msg


async def test_start_frontend_idempotent_when_running(tmp_path, monkeypatch):
    def _no_spawn(*a, **k):
        raise AssertionError("should not spawn when a front-end is already running")
    monkeypatch.setattr(launcher.subprocess, "Popen", _no_spawn)
    ctx = _fe_ctx(tmp_path)
    ctx.voice_frontend_process = FakeProc(poll_value=None)  # alive
    ok, msg = await launcher.start_frontend(ctx, 8783)
    assert ok is True and "already running" in msg


def test_stop_frontend_terminates(tmp_path):
    ctx = _fe_ctx(tmp_path)
    proc = FakeProc()
    ctx.voice_frontend_process = proc
    launcher.stop_frontend(ctx)
    assert proc.terminated is True
    assert ctx.voice_frontend_process is None
