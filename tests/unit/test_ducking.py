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


# -- browser-played movie: duck VOLUME, never pause (no REST, no Space toggle to fight) ----------

class _FakeVideoPage:
    """Models the HTML5 <video> we drive via page.evaluate."""
    def __init__(self, volume=0.9, paused=False):
        self.volume = volume; self.paused = paused; self._closed = False
    def is_closed(self): return self._closed
    async def evaluate(self, expr, arg=None):
        if "v.volume = vol" in expr: self.volume = arg; return None
        if "v.paused" in expr: return self.paused
        if "v.volume" in expr: return self.volume
        return None


@respx.mock
async def test_duck_jellyfin_browser_lowers_volume_not_pause():
    # respx with NO Sessions/Pause routes: if it tried REST it would raise → proves it didn't.
    ctx = _ctx(tidal=False, jellyfin=True)
    page = _FakeVideoPage(volume=0.9)
    ctx.jellyfin_playing_page = page
    assert (await duck_media(ctx, True))["ducked"] == ["jellyfin"]
    assert page.volume == 0.2                       # ducked the element volume
    assert (await duck_media(ctx, False))["ducked"] == ["jellyfin"]
    assert page.volume == 0.9                       # restored the EXACT prior level


@respx.mock
async def test_duck_jellyfin_browser_idempotent():
    ctx = _ctx(tidal=False, jellyfin=True)
    page = _FakeVideoPage(volume=0.7); ctx.jellyfin_playing_page = page
    await duck_media(ctx, True)
    out = await duck_media(ctx, True)               # second duck while already ducked
    assert out["ducked"] == [] and page.volume == 0.2   # prior (0.7) preserved, not clobbered with 0.2
    await duck_media(ctx, False)
    assert page.volume == 0.7


@respx.mock
async def test_duck_jellyfin_stranded_low_restores_to_full():
    # The movie was left at the duck level (0.2) by a prior restart. Ducking must NOT save 0.2 as the
    # restore target (that strands it quiet forever) — it saves full so restore un-stutters it.
    from gabagent.voice.ducking import _state
    ctx = _ctx(tidal=False, jellyfin=True)
    page = _FakeVideoPage(volume=0.2); ctx.jellyfin_playing_page = page
    await duck_media(ctx, True)
    assert _state(ctx)["jellyfin_video_volume"] == 1.0     # saved FULL, not the stranded 0.2
    await duck_media(ctx, False)
    assert page.volume == 1.0                              # restored to full


class _FailingSetPage(_FakeVideoPage):
    """The volume SETTER silently fails (page-eval error), like a dead/navigated page."""
    async def evaluate(self, expr, arg=None):
        if "v.volume = vol" in expr:
            raise RuntimeError("eval failed")
        return await super().evaluate(expr, arg)


@respx.mock
async def test_duck_restore_failure_keeps_saved_level_for_retry():
    # The bug that stranded the movie quiet: a failed restore used to wipe the saved level, so the
    # NEXT duck read the still-low video and saved 0.2 as "prior". Now a failed restore keeps it.
    from gabagent.voice.ducking import _state
    ctx = _ctx(tidal=False, jellyfin=True)
    page = _FailingSetPage(volume=0.9); ctx.jellyfin_playing_page = page
    await duck_media(ctx, True)
    st = _state(ctx)
    assert st["jellyfin_video_volume"] == 0.9          # prior saved
    out = await duck_media(ctx, False)                 # restore set() fails
    assert out["ducked"] == []                         # not reported as restored
    assert st["jellyfin_video_volume"] == 0.9          # KEPT (not wiped) so a later off can retry


# -- GET /media/state snapshot ---------------------------------------------

@respx.mock
async def test_media_state_browser_playing_and_music():
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=True, jellyfin=True)
    respx.post(RPC).mock(side_effect=_mopidy([], state="playing"))
    ctx.jellyfin_playing_page = _FakeVideoPage(paused=False)
    assert await media_state(ctx) == {"playing": True, "state": "playing"}


@respx.mock
async def test_media_state_paused_video_stopped_music():
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=True, jellyfin=True)
    respx.post(RPC).mock(side_effect=_mopidy([], state="stopped"))
    ctx.jellyfin_playing_page = _FakeVideoPage(paused=True)
    assert await media_state(ctx) == {"playing": False, "state": "paused"}


@respx.mock
async def test_media_state_nothing_playing():
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=False, jellyfin=True)
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    st = await media_state(ctx)
    assert st == {"playing": False, "state": "idle"}


@respx.mock
async def test_media_state_is_provider_neutral():
    # PHILOSOPHY GUARD: the brain↔voice protocol must not leak brain-specific provider names. If a new
    # provider is added, the /media/state shape must stay generic (aggregate), never grow per-provider
    # keys — otherwise a different brain stops being pluggable.
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=True, jellyfin=True)
    respx.post(RPC).mock(side_effect=_mopidy([], state="playing"))
    ctx.jellyfin_playing_page = _FakeVideoPage(paused=False)
    st = await media_state(ctx)
    assert set(st.keys()) <= {"playing", "state"}              # neutral keys only
    assert not any(k in str(st).lower() for k in ("jellyfin", "tidal", "mopidy", "video"))
