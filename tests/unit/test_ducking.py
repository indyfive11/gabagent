import json
import types
import httpx
import pytest
import respx

from gabagent.config.models import GabAgentConfig
from gabagent.voice import ducking as _dk
from gabagent.voice.ducking import duck_media


@pytest.fixture(autouse=True)
def _no_real_pactl(monkeypatch):
    """Never touch real system audio in tests — stub pactl so the sink-input duck no-ops by default
    (rc=1 → _mopidy_sink_input None). Explicit sink tests override this."""
    async def _fake(*a, **k):
        return (1, "")
    monkeypatch.setattr(_dk, "_run_pactl", _fake)

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


# -- music duck: belt-and-suspenders PipeWire sink-input (the reliably-audible system node) ---------

def test_parse_mopidy_sink_input():
    from gabagent.voice.ducking import _parse_mopidy_sink_input
    out = (
        'Sink Input #5\n'
        '\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100%\n'
        '\tProperties:\n\t\tapplication.name = "Firefox"\n'
        'Sink Input #12\n'
        '\tVolume: front-left: 45000 / 69% / -3.00 dB\n'
        '\tProperties:\n\t\tapplication.name = "Mopidy"\n'
    )
    assert _parse_mopidy_sink_input(out) == ("12", 69)        # picks the Mopidy stream, parses %
    assert _parse_mopidy_sink_input("nothing here") is None


async def test_duck_tidal_sink_ducks_then_restores(monkeypatch):
    calls = []
    list_out = ('Sink Input #7\n\tVolume: front-left: 58000 / 90% / -1.0 dB\n'
                '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
    async def fake_pactl(*args, **k):
        calls.append(args)
        return (0, list_out) if args and args[0] == "list" else (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)

    ctx = types.SimpleNamespace()
    await _dk._duck_tidal_sink(ctx, True)
    st = _dk._state(ctx)
    assert st["tidal_sink_prior"] == ("7", 90)                # saved real prior
    assert ("set-sink-input-volume", "7", "18%") in calls     # ducked the system node

    calls.clear()
    await _dk._duck_tidal_sink(ctx, False)
    assert st["tidal_sink_prior"] is None
    assert ("set-sink-input-volume", "7", "90%") in calls     # restored to the saved prior


async def test_duck_tidal_sink_noop_when_no_mopidy_stream(monkeypatch):
    async def fake_pactl(*args, **k):
        return (0, 'Sink Input #1\n\tProperties:\n\t\tapplication.name = "Firefox"\n')
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace()
    await _dk._duck_tidal_sink(ctx, True)
    assert _dk._state(ctx)["tidal_sink_prior"] is None        # nothing to duck → no state saved


async def test_duck_tidal_sink_stranded_low_restores_to_cap(monkeypatch):
    """Sink-input already at/below the duck level (leftover from a restart mid-duck) → save the ambient
    cap as the restore target, not the stranded 18% (mirrors the video stranded-0.2 guard)."""
    async def fake_pactl(*args, **k):
        if args and args[0] == "list":
            return (0, 'Sink Input #3\n\tVolume: front-left: 11796 / 18% / -20 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk._duck_tidal_sink(ctx, True)
    assert _dk._state(ctx)["tidal_sink_prior"] == ("3", 90)    # the cap, not the stranded 18 or full 100


# -- ambient cap: hold playing music at a ceiling so VAD can hear the user over it -----------------

async def test_apply_ambient_cap_lowers_only(monkeypatch):
    calls = []
    async def fake_pactl(*a, **k):
        if a and a[0] == "list":
            return (0, 'Sink Input #9\n\tVolume: front-left: 65536 / 100% / 0 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    seen = []
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        seen.append((method, params)); return 100 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk.apply_ambient_cap(ctx)
    assert ("core.mixer.set_volume", {"volume": 90}) in seen     # mixer lowered to the cap
    assert ("set-sink-input-volume", "9", "90%") in calls         # sink lowered to the cap


async def test_apply_ambient_cap_disabled_at_100(monkeypatch):
    calls = []
    async def fake_pactl(*a, **k): calls.append(a); return (1, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=100))
    await _dk.apply_ambient_cap(ctx)
    assert calls == []                                            # cap=100 disables → no system calls


@respx.mock
async def test_duck_tidal_restore_caps_above_ambient():
    """Music ducked from a pre-cap 100 restores to the 90% ambient cap, not back to 100."""
    ctx = _ctx(tidal=True, jellyfin=False)               # media_ambient_cap defaults to 90
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=100))
    await duck_media(ctx, True)                           # save 100, duck to 18
    seen.clear()
    await duck_media(ctx, False)                          # restore → min(100, 90) = 90
    assert ("core.mixer.set_volume", {"volume": 90}) in seen
    assert ("core.mixer.set_volume", {"volume": 100}) not in seen
