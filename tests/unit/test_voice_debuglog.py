import json
import types
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.voice.debuglog import dlog
from gabagent.voice.session import VoiceSession
from gabagent.permissions.voice_approve import voice_approve


def _read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _ctx(tmp_path, debug=True, **kw):
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(
        config=cfg,
        cwd=tmp_path,
        local_mode=False,
        voice_emit=None,
        voice_session=VoiceSession("sess-x", None),
        voice_audit_path=None,
        voice_debug_path=(tmp_path / "voice_debug.jsonl") if debug else None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def test_dlog_writes_keyed_by_session(tmp_path):
    ctx = _ctx(tmp_path)
    dlog(ctx, "tier", tool="bash", tier=3, method="keyboard")
    rows = _read_lines(ctx.voice_debug_path)
    assert len(rows) == 1
    e = rows[0]
    assert e["session"] == "sess-x" and e["event"] == "tier"
    assert e["tool"] == "bash" and e["tier"] == 3 and e["method"] == "keyboard"
    assert "ts" in e


def test_dlog_noop_when_disabled(tmp_path):
    ctx = _ctx(tmp_path, debug=False)
    dlog(ctx, "tier", tool="bash", tier=3)  # must not raise
    assert not (tmp_path / "voice_debug.jsonl").exists()


def test_dlog_drops_none_fields(tmp_path):
    ctx = _ctx(tmp_path)
    dlog(ctx, "tool", name="grep", ok=True, error=None)
    e = _read_lines(ctx.voice_debug_path)[0]
    assert "error" not in e and e["ok"] is True


async def test_voice_approve_logs_tier_for_read(tmp_path):
    ctx = _ctx(tmp_path)
    assert await voice_approve("read_file", {"path": "x"}, ctx) is True
    rows = _read_lines(ctx.voice_debug_path)
    tiers = [r for r in rows if r["event"] == "tier"]
    assert tiers and tiers[0]["tool"] == "read_file" and tiers[0]["method"] == "auto"


async def test_voice_approve_logs_decision_denied(tmp_path):
    ctx = _ctx(tmp_path)

    async def emit(ev):
        if ev.type == "confirm":
            ctx.voice_session.resolve(ev.id, False)

    ctx.voice_emit = emit
    assert await voice_approve("bash", {"command": "rm -rf x"}, ctx) is False
    rows = _read_lines(ctx.voice_debug_path)
    events = {r["event"] for r in rows}
    assert "tier" in events and "decision" in events
    decision = next(r for r in rows if r["event"] == "decision")
    assert decision["decision"] == "denied" and decision["tier"] == 3
