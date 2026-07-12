"""P1 — `system.fix_audio` active audio recovery. Idempotently unmute the DEFAULT SINK and raise it
to a 50% floor if below, instead of the blind `system.mute` toggle that can mute an already-live sink.
HARD INVARIANT under test: it writes ONLY the default sink, never a sink-input (VAC owns the TTS
sink-input via its pid pin; the two composite, so a second writer on the sink-input would collide)."""
import types

from gabagent.commands.providers import system as sysprov


def _patch(monkeypatch, *, sink="alsa_output.default", muted=None, vol=None, calls=None):
    async def _dsn():
        return sink

    async def _smv(s):
        return (muted, vol)

    async def _pactl(*args, timeout=2.0):
        if calls is not None:
            calls.append(args)
        return (0, "")

    monkeypatch.setattr("gabagent.voice.ducking._default_sink_name", _dsn)
    monkeypatch.setattr("gabagent.voice.ducking._sink_mute_volume", _smv)
    monkeypatch.setattr("gabagent.voice.ducking._run_pactl", _pactl)


async def test_fix_audio_unmutes_a_muted_sink(monkeypatch):
    calls = []
    _patch(monkeypatch, muted=True, vol=80, calls=calls)
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert res.success and "unmuted" in res.output
    assert ("set-sink-mute", "alsa_output.default", "0") in calls  # idempotent 0, never toggle
    assert not any("toggle" in a for a in calls)


async def test_fix_audio_raises_low_volume_to_floor(monkeypatch):
    calls = []
    _patch(monkeypatch, muted=False, vol=10, calls=calls)
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert res.success and "50%" in res.output
    assert ("set-sink-volume", "alsa_output.default", "50%") in calls


async def test_fix_audio_muted_and_low_does_both(monkeypatch):
    _patch(monkeypatch, muted=True, vol=5)
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert "unmuted" in res.output and "50%" in res.output


async def test_fix_audio_noop_when_already_fine(monkeypatch):
    calls = []
    _patch(monkeypatch, muted=False, vol=80, calls=calls)
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert res.success and "already" in res.output
    assert calls == []  # nothing written when audio is already unmuted and up


async def test_fix_audio_does_not_raise_volume_at_or_above_floor(monkeypatch):
    calls = []
    _patch(monkeypatch, muted=False, vol=50, calls=calls)  # exactly the floor → leave it
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert res.success and calls == []


async def test_fix_audio_never_writes_a_sink_input(monkeypatch):
    # INVARIANT (i): fix_audio must touch ONLY the default sink, never a sink-input.
    calls = []
    _patch(monkeypatch, muted=True, vol=1, calls=calls)
    await sysprov.fix_audio(types.SimpleNamespace())
    assert calls  # it did write
    for a in calls:
        assert not any("sink-input" in str(x) for x in a)
        assert a[1] == "alsa_output.default"  # every write targets the default sink by name


async def test_fix_audio_errors_without_default_sink(monkeypatch):
    _patch(monkeypatch, sink=None)
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert not res.success and "audio output" in res.error


async def test_fix_audio_errors_when_state_unreadable(monkeypatch):
    calls = []
    _patch(monkeypatch, muted=None, vol=None, calls=calls)  # both unreadable, not muted-False
    res = await sysprov.fix_audio(types.SimpleNamespace())
    assert not res.success and "couldn't read" in res.error
    assert calls == []  # never wrote on an unreadable device


def test_fix_audio_command_published_only_with_pactl(monkeypatch):
    # The capability appears in the catalog when pactl is present, and is a dedicated PyBackend
    # (not folded into a read/status path), user-invoked via the "can't hear" examples.
    monkeypatch.setattr(sysprov.shutil, "which", lambda b: b == "pactl")
    cmds = sysprov.SystemProvider().commands(types.SimpleNamespace())
    fa = [c for c in cmds if c.id == "system.fix_audio"]
    assert len(fa) == 1
    assert fa[0].backend.ref == "gabagent.commands.providers.system:fix_audio"
    assert any("can't hear" in e for e in fa[0].examples)


def test_fix_audio_absent_without_pactl(monkeypatch):
    monkeypatch.setattr(sysprov.shutil, "which", lambda b: b == "wpctl")
    cmds = sysprov.SystemProvider().commands(types.SimpleNamespace())
    assert not any(c.id == "system.fix_audio" for c in cmds)  # pactl-only for now (wpctl = follow-up)
