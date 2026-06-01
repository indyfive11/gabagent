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
