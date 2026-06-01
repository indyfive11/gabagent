import types
import time
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.voice.session import VoiceSession
from gabagent.permissions.voice_approve import voice_approve, _summarize, _reason


def _ctx(tmp_path, **kw):
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(
        config=cfg, cwd=tmp_path, local_mode=False,
        voice_emit=None, voice_session=None, voice_audit_path=None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def test_summarize_write_reports_lines(tmp_path):
    ctx = _ctx(tmp_path)
    s = _summarize("write_file", {"path": "x.txt", "content": "a\nb\nc"}, ctx)
    assert "3 lines" in s


def test_summarize_destructive_is_loud(tmp_path):
    ctx = _ctx(tmp_path)
    s = _summarize("bash", {"command": "rm -rf foo"}, ctx)
    assert "permanent" in s.lower()


def test_reason_vpn_only_when_networked(tmp_path):
    net = _ctx(tmp_path, local_mode=False)
    assert "network" in _reason("bash", {"command": "vpn-full"}, 3, net).lower()
    local = _ctx(tmp_path, local_mode=True)
    assert "network" not in _reason("bash", {"command": "vpn-full"}, 3, local).lower()


async def test_tier3_arming_skips_second_prompt(tmp_path):
    vs = VoiceSession("s", None)
    emitted = []

    async def emit(ev):
        emitted.append(ev)
        if ev.type == "confirm":
            vs.resolve(ev.id, True)

    ctx = _ctx(tmp_path, voice_session=vs, voice_emit=emit)

    r1 = await voice_approve("bash", {"command": "echo one"}, ctx)
    assert r1 is True
    assert any(e.type == "confirm" for e in emitted)

    emitted.clear()
    r2 = await voice_approve("bash", {"command": "echo two"}, ctx)
    assert r2 is True
    assert not any(e.type == "confirm" for e in emitted)   # armed → no new prompt
    assert any(e.type == "status" for e in emitted)

    # Expire the arming window → re-prompt.
    vs.armed["shell"] = time.time() - 1
    emitted.clear()
    await voice_approve("bash", {"command": "echo three"}, ctx)
    assert any(e.type == "confirm" for e in emitted)


async def test_confirm_timeout_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr("gabagent.permissions.voice_approve._CONFIRM_TIMEOUT", 0.05)
    vs = VoiceSession("s", None)
    emitted = []

    async def emit(ev):
        emitted.append(ev)   # never resolve → should time out

    ctx = _ctx(tmp_path, voice_session=vs, voice_emit=emit)
    result = await voice_approve("bash", {"command": "echo hi"}, ctx)
    assert result is False


async def test_tier1_read_auto_approves(tmp_path):
    ctx = _ctx(tmp_path, voice_session=VoiceSession("s", None))
    assert await voice_approve("read_file", {"path": "x"}, ctx) is True
