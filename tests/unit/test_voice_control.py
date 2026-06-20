"""voice.set_volume — the F3 my-voice-volume command + its voice_volume SSE event."""
import types
import pytest

from gabagent.commands.providers import voice_control as vc
from gabagent.voice import events


def _ctx(voice_mode=True):
    return types.SimpleNamespace(voice_mode=voice_mode)


async def test_detect_only_in_voice_mode():
    assert await vc.PROVIDER.detect(_ctx(voice_mode=True)) is True
    assert await vc.PROVIDER.detect(_ctx(voice_mode=False)) is False
    assert await vc.PROVIDER.detect(types.SimpleNamespace()) is False  # no attr → not available


def test_command_shape():
    cmds = {c.id: c for c in vc.PROVIDER.commands(_ctx())}
    assert "voice.set_volume" in cmds
    c = cmds["voice.set_volume"]
    assert c.domain == "voice"           # NOT media/system — its own domain, so media keepalive doesn't fire
    assert c.tier == 1 and c.featured is True
    op = next(s for s in c.params if s.name == "op")
    assert op.required is True and op.type == "enum" and set(op.enum) == {"up", "down", "set"}
    # The disambiguation from media volume must live in the summary (the model routes on it).
    assert "not the music" in c.summary.lower() or "not the music or movie" in c.summary.lower()


async def test_set_volume_records_signal_and_confirms():
    ctx = _ctx()
    res = await vc.set_volume(ctx, op="down")
    assert ctx._voice_volume_signal == {"op": "down"}
    assert res.error is None and "lower" in res.output.lower()


async def test_bad_op_falls_back_to_down():
    ctx = _ctx()
    await vc.set_volume(ctx, op="LOUDER-ish garbage")
    assert ctx._voice_volume_signal == {"op": "down"}


async def test_set_with_value_clamps_to_unit_range():
    ctx = _ctx()
    await vc.set_volume(ctx, op="set", value=0.3)
    assert ctx._voice_volume_signal == {"op": "set", "value": 0.3}
    await vc.set_volume(ctx, op="set", value=5)        # over-range
    assert ctx._voice_volume_signal == {"op": "set", "value": 1.0}
    await vc.set_volume(ctx, op="set", value=-2)       # under-range
    assert ctx._voice_volume_signal == {"op": "set", "value": 0.0}


async def test_up_and_set_without_value():
    ctx = _ctx()
    await vc.set_volume(ctx, op="up")
    assert ctx._voice_volume_signal == {"op": "up"}    # no value key when none given


def test_event_wire_shape():
    assert events.voice_volume("down").to_dict() == {"type": "voice_volume", "op": "down"}
    assert events.voice_volume("up").to_dict() == {"type": "voice_volume", "op": "up"}
    # value present and explicit — including 0.0, which the empty-value filter must NOT strip (it rides `extra`).
    assert events.voice_volume("set", 0.3).to_dict() == {"type": "voice_volume", "op": "set", "value": 0.3}
    assert events.voice_volume("set", 0.0).to_dict() == {"type": "voice_volume", "op": "set", "value": 0.0}
