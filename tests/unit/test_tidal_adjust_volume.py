"""tidal.adjust_volume — RELATIVE music-volume bump (the 'turn it up and nothing changed' fix).

Covers: bump from the live mixer when no duck is open; bump from the saved pre-duck prior (NOT the ducked
mixer) while a command window is open; ceiling/floor edges; custom amount; and that absolute set_volume is
unchanged. Mirrors test_ducking's respx/_ctx harness."""
import json
import types
import httpx
import pytest
import respx

from gabagent.config.models import GabAgentConfig
from gabagent.voice import ducking as _dk
from gabagent.commands.providers import tidal as _t

RPC = "http://mopidy.test:6680/mopidy/rpc"


@pytest.fixture(autouse=True)
def _no_real_pactl(monkeypatch):
    """Never touch real audio: stub the sink-input duck so _set_stream_volume's pactl leg no-ops."""
    async def _fake_pactl(*a, **k):
        return (1, "")
    async def _no_sink():
        return None
    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl)
    monkeypatch.setattr(_dk, "_mopidy_sink_input", _no_sink)


def _ctx(step=15):
    cfg = GabAgentConfig(api_key="test")
    cfg.tidal.enabled = True
    cfg.tidal.rpc_url = RPC
    cfg.media_volume_step = step
    return types.SimpleNamespace(config=cfg)


def _mopidy(seen, vol=80):
    def _resp(request):
        m = json.loads(request.content)["method"]
        seen.append((m, json.loads(request.content).get("params")))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"core.mixer.get_volume": vol}.get(m)})
    return _resp


@respx.mock
async def test_up_bumps_from_live_mixer():
    ctx = _ctx()
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    res = await _t.adjust_volume(ctx, direction="up")
    assert ("core.mixer.set_volume", {"volume": 95}) in seen      # 80 + 15
    assert "95" in res.output and not res.error


@respx.mock
async def test_down_bumps_from_live_mixer():
    ctx = _ctx()
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    res = await _t.adjust_volume(ctx, direction="down")
    assert ("core.mixer.set_volume", {"volume": 65}) in seen      # 80 - 15
    assert "65" in res.output


@respx.mock
async def test_custom_amount():
    ctx = _ctx()
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=50))
    await _t.adjust_volume(ctx, direction="up", amount=30)
    assert ("core.mixer.set_volume", {"volume": 80}) in seen      # 50 + 30


@respx.mock
async def test_up_during_open_duck_reads_prior_not_ducked_mixer():
    """The crux: while a command window is open the live mixer reads the ducked ~18%, so the bump MUST be
    computed off the saved pre-duck prior — and applied via note_user_volume (restore-prior update), NOT a
    live set that would leak loud bed into the mic VAD."""
    ctx = _ctx()
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=18))       # mixer is ducked
    # Simulate an open duck window with the user's chosen level saved at 80.
    st = _dk._state(ctx)
    st["tidal_prior"] = 80
    res = await _t.adjust_volume(ctx, direction="up")
    # No live mixer write (would leak); the new level lands on the restore prior.
    assert not any(m == "core.mixer.set_volume" for m, _ in seen)
    assert _dk.pending_user_volume(ctx) == 95                     # 80 + 15, recorded for restore
    assert "95" in res.output


@respx.mock
async def test_up_at_ceiling_is_a_noop_with_friendly_word():
    ctx = _ctx()
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=100))
    res = await _t.adjust_volume(ctx, direction="up")
    assert not any(m == "core.mixer.set_volume" for m, _ in seen)
    assert "already" in res.output.lower()


async def test_bad_direction_errors():
    ctx = _ctx()
    res = await _t.adjust_volume(ctx, direction="sideways")
    assert res.error and not res.output


@respx.mock
async def test_set_volume_absolute_unchanged():
    ctx = _ctx()
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    res = await _t.set_volume(ctx, level=30)
    assert ("core.mixer.set_volume", {"volume": 30}) in seen
    assert "30" in res.output


def test_adjust_volume_command_registered():
    from gabagent.commands.providers.tidal import TidalProvider
    cmds = {c.id: c for c in TidalProvider().commands(_ctx())}
    assert "tidal.adjust_volume" in cmds
    c = cmds["tidal.adjust_volume"]
    d = {s.name: s for s in c.params}
    assert d["direction"].enum == ("up", "down")
