import json
import types
import httpx
import respx

from gabagent.config.models import GabAgentConfig
from gabagent.voice.ducking import duck_media

RPC = "http://mopidy.test:6680/mopidy/rpc"
BASE = "http://jf.test:8096"


def _ctx(tidal=True, jellyfin=False):
    cfg = GabAgentConfig(api_key="test")
    cfg.tidal.enabled = tidal
    cfg.tidal.rpc_url = RPC
    cfg.jellyfin.enabled = True
    cfg.jellyfin.api_key = "k" if jellyfin else ""
    cfg.jellyfin.base_url = BASE
    return types.SimpleNamespace(config=cfg)


def _mopidy(seen, state="playing", vol=80):
    def _resp(request):
        m = json.loads(request.content)["method"]
        seen.append((m, json.loads(request.content).get("params")))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result":
                                         {"core.playback.get_state": state,
                                          "core.mixer.get_volume": vol}.get(m)})
    return _resp


@respx.mock
async def test_duck_tidal_lowers_then_restores():
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen))

    out = await duck_media(ctx, True)
    assert out["ducked"] == ["tidal"]
    assert ("core.mixer.set_volume", {"volume": 18}) in seen     # ducked to the low level

    out2 = await duck_media(ctx, False)
    assert out2["ducked"] == ["tidal"]
    assert ("core.mixer.set_volume", {"volume": 80}) in seen     # restored to the exact prior level


@respx.mock
async def test_duck_tidal_noop_when_not_playing():
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, state="stopped"))
    out = await duck_media(ctx, True)
    assert out["ducked"] == []                                   # nothing playing → no-op
    assert not any(m == "core.mixer.set_volume" for m, _ in seen)


@respx.mock
async def test_duck_is_idempotent():
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen))
    await duck_media(ctx, True)
    seen.clear()
    out = await duck_media(ctx, True)                            # second duck while already ducked
    assert out["ducked"] == [] and not seen                      # no RPC, prior level preserved


@respx.mock
async def test_duck_jellyfin_pauses_then_resumes():
    ctx = _ctx(tidal=False, jellyfin=True)
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "The Matrix"}, "PlayState": {"IsPaused": False}},
    ]))
    pause = respx.post(f"{BASE}/Sessions/s1/Playing/Pause").mock(return_value=httpx.Response(204))
    unpause = respx.post(f"{BASE}/Sessions/s1/Playing/Unpause").mock(return_value=httpx.Response(204))

    assert (await duck_media(ctx, True))["ducked"] == ["jellyfin"]
    assert pause.called
    assert (await duck_media(ctx, False))["ducked"] == ["jellyfin"]
    assert unpause.called


async def test_duck_noop_when_nothing_configured():
    ctx = _ctx(tidal=False, jellyfin=False)
    assert await duck_media(ctx, True) == {"ok": True, "ducked": []}
