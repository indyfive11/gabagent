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
async def test_duck_does_not_autopause_unowned_session():
    # ARCHITECTURE: an unowned Jellyfin /Sessions client (it may be on ANOTHER device/room) must NEVER be
    # auto-paused when the user speaks — doing so controlled the wrong screen and flapped the gate. Only
    # brain-OWNED media is auto-ducked. (Cross-device transport is Phase-2 explicit-only.)
    ctx = _ctx(tidal=False, jellyfin=True)
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "The Matrix"}, "PlayState": {"IsPaused": False}},
    ]))
    pause = respx.post(f"{BASE}/Sessions/s1/Playing/Pause").mock(return_value=httpx.Response(204))

    assert (await duck_media(ctx, True))["ducked"] == []      # nothing OWNED → nothing ducked
    assert not pause.called                                   # the unowned session was left untouched
    assert (await duck_media(ctx, False))["ducked"] == []


async def test_duck_noop_when_nothing_configured():
    ctx = _ctx(tidal=False, jellyfin=False)
    assert await duck_media(ctx, True) == {"ok": True, "ducked": []}


async def test_duck_window_lifecycle_flag_and_seq():
    """The duck-window flag opens on the first duck-on, stays open across repeat ons, and closes on duck-off
    — and the window sequence advances per window. This is what the mid-duck reconcile keys its level
    decision off (and what surfaces a window stuck open)."""
    ctx = _ctx(tidal=False, jellyfin=False)
    st = _dk._state(ctx)
    await duck_media(ctx, True)
    assert st["duck_window_open"] is True and st["duck_window_seq"] == 1
    await duck_media(ctx, True)                       # a second onset within the same window
    assert st["duck_window_open"] is True and st["duck_window_seq"] == 1   # same window, not a new one
    await duck_media(ctx, False)
    assert st["duck_window_open"] is False
    await duck_media(ctx, True)                       # a fresh window
    assert st["duck_window_open"] is True and st["duck_window_seq"] == 2


# -- browser-played movie: duck VOLUME, never pause (no REST, no Space toggle to fight) ----------

class _FakeVideoPage:
    """Models the HTML5 <video> we drive via page.evaluate."""
    def __init__(self, volume=0.9, paused=False):
        self.volume = volume; self.paused = paused; self._closed = False
    def is_closed(self): return self._closed
    async def evaluate(self, expr, arg=None):
        if "v.volume = vol" in expr: self.volume = arg; return True   # set-JS returns true on success
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
    assert await media_state(ctx) == {"playing": True, "state": "playing", "kind": "video"}


@respx.mock
async def test_media_state_paused_video_stopped_music():
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=True, jellyfin=True)
    respx.post(RPC).mock(side_effect=_mopidy([], state="stopped"))
    ctx.jellyfin_playing_page = _FakeVideoPage(paused=True)
    assert await media_state(ctx) == {"playing": False, "state": "paused", "kind": "video"}


@respx.mock
async def test_media_state_kind_audio_when_only_music_plays():
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=True, jellyfin=False)
    respx.post(RPC).mock(side_effect=_mopidy([], state="playing"))
    assert await media_state(ctx) == {"playing": True, "state": "playing", "kind": "audio"}


@respx.mock
async def test_media_state_nothing_playing():
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=False, jellyfin=True)
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    st = await media_state(ctx)
    assert st == {"playing": False, "state": "idle", "kind": None}


@respx.mock
async def test_media_state_is_provider_neutral():
    # PHILOSOPHY GUARD: the brain↔voice protocol must not leak brain-specific PROVIDER names. The shape
    # stays generic (aggregate) — `kind` is a generic media TYPE ("audio"/"video"), which any brain could
    # report, NOT a provider — so a different brain stays pluggable.
    from gabagent.voice.ducking import media_state
    ctx = _ctx(tidal=True, jellyfin=True)
    respx.post(RPC).mock(side_effect=_mopidy([], state="playing"))
    ctx.jellyfin_playing_page = _FakeVideoPage(paused=False)
    st = await media_state(ctx)
    assert set(st.keys()) <= {"playing", "state", "kind"}          # generic keys only
    assert st["kind"] in ("audio", "video", None)                  # a media type, not a provider
    assert not any(k in str(st).lower() for k in ("jellyfin", "tidal", "mopidy"))  # no provider names


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
    assert ("set-sink-input-mute", "7", "0") in calls         # and cleared any stray mute (silence guard)


def test_parse_mopidy_sink_full():
    from gabagent.voice.ducking import _parse_mopidy_sink_full
    out = (
        'Sink Input #5\n\tVolume: front-left: 65536 / 100%\n'
        '\tMute: no\n\tProperties:\n\t\tapplication.name = "Firefox"\n'
        'Sink Input #12\n\tVolume: front-left: 45000 / 69% / -3.00 dB\n'
        '\tMute: yes\n\tSink: 3\n\tProperties:\n\t\tapplication.name = "Mopidy"\n'
    )
    assert _parse_mopidy_sink_full(out) == {"idx": "12", "volume": 69, "muted": True, "sink_idx": "3"}
    assert _parse_mopidy_sink_full("nothing here") is None


def _pactl_router(monkeypatch, *, sink_inputs="", default_sink="", sinks_short=""):
    """Stub pactl + shutil.which for the audibility probe: route by the pactl subcommand."""
    monkeypatch.setattr(_dk.shutil, "which", lambda _name: "/usr/bin/pactl")
    async def fake_pactl(*args, **k):
        if args[:1] == ("get-default-sink",):
            return (0, default_sink)
        if args[:3] == ("list", "short", "sinks"):
            return (0, sinks_short)
        if args[:2] == ("list", "sink-inputs"):
            return (0, sink_inputs)
        return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)


_AUDIBLE_BLOCK = ('Sink Input #12\n\tVolume: front-left: 45000 / 69%\n'
                  '\tMute: no\n\tSink: 3\n\tProperties:\n\t\tapplication.name = "Mopidy"\n')


async def test_mopidy_audibility_true_when_unmuted_nonzero_on_default(monkeypatch):
    _pactl_router(monkeypatch, sink_inputs=_AUDIBLE_BLOCK,
                  default_sink="alsa.spk", sinks_short="3\talsa.spk\tmod\trun\n")
    res = await _dk.mopidy_audibility()
    assert res["checked"] and res["present"] and res["audible"] is True
    assert "reason" not in res


async def test_mopidy_audibility_false_when_muted(monkeypatch):
    blk = _AUDIBLE_BLOCK.replace("Mute: no", "Mute: yes")
    _pactl_router(monkeypatch, sink_inputs=blk, default_sink="alsa.spk",
                  sinks_short="3\talsa.spk\tmod\trun\n")
    res = await _dk.mopidy_audibility()
    assert res["audible"] is False and "muted" in res["reason"]


async def test_mopidy_audibility_false_when_zero_volume(monkeypatch):
    blk = _AUDIBLE_BLOCK.replace("69%", "0%")
    _pactl_router(monkeypatch, sink_inputs=blk, default_sink="alsa.spk",
                  sinks_short="3\talsa.spk\tmod\trun\n")
    res = await _dk.mopidy_audibility()
    assert res["audible"] is False and "zero" in res["reason"]


async def test_mopidy_audibility_false_when_off_default_sink(monkeypatch):
    # Mopidy is on sink 3 (hdmi) but the default sink is the speakers → routed elsewhere → inaudible.
    _pactl_router(monkeypatch, sink_inputs=_AUDIBLE_BLOCK, default_sink="alsa.spk",
                  sinks_short="3\talsa.hdmi\tmod\trun\n9\talsa.spk\tmod\trun\n")
    res = await _dk.mopidy_audibility()
    assert res["audible"] is False and "alsa.hdmi" in res["reason"]


async def test_mopidy_audibility_present_false_when_no_stream(monkeypatch):
    _pactl_router(monkeypatch, sink_inputs='Sink Input #5\n\tProperties:\n\t\tapplication.name = "Firefox"\n')
    res = await _dk.mopidy_audibility()
    assert res["checked"] and res["present"] is False and res["audible"] is False


async def test_mopidy_audibility_unchecked_when_no_pactl(monkeypatch):
    monkeypatch.setattr(_dk.shutil, "which", lambda _name: None)
    res = await _dk.mopidy_audibility()
    assert res == {"checked": False}


async def test_duck_tidal_sink_noop_when_no_mopidy_stream(monkeypatch):
    async def fake_pactl(*args, **k):
        return (0, 'Sink Input #1\n\tProperties:\n\t\tapplication.name = "Firefox"\n')
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace()
    await _dk._duck_tidal_sink(ctx, True)
    assert _dk._state(ctx)["tidal_sink_prior"] is None        # nothing to duck → no state saved


async def test_duck_tidal_sink_prior_from_mixer(monkeypatch):
    """The sink restore prior MIRRORS the Mopidy software mixer (the source of truth), threaded in by
    _duck_tidal — even when the sink itself reads low. We no longer fabricate the ambient cap for a
    low sink (that overrode a deliberately-low user level → the 'turn it down → blasts back to 90' bug);
    a genuinely stranded sink is un-stranded separately by apply_ambient_cap's mirror-on-play."""
    calls = []
    async def fake_pactl(*args, **k):
        if args and args[0] == "list":
            return (0, 'Sink Input #3\n\tVolume: front-left: 11796 / 18% / -20 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        calls.append(args)
        return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk._duck_tidal_sink(ctx, True, mixer_vol=55)        # mixer says the real level is 55
    assert _dk._state(ctx)["tidal_sink_prior"] == ("3", 55)    # mirrors the mixer, NOT the fabricated cap
    calls.clear()
    await _dk._duck_tidal_sink(ctx, False)
    assert ("set-sink-input-volume", "3", "55%") in calls      # restored to the user level, not 90


async def test_duck_tidal_sink_no_mixer_uses_own_read(monkeypatch):
    """Standalone call (no mixer threaded) falls back to the sink's HONEST read — no cap fabrication.
    The old code rescued an 18% sink to 90; now it saves 18 (un-stranding is apply_ambient_cap's job)."""
    async def fake_pactl(*args, **k):
        if args and args[0] == "list":
            return (0, 'Sink Input #3\n\tVolume: front-left: 11796 / 18% / -20 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk._duck_tidal_sink(ctx, True)
    assert _dk._state(ctx)["tidal_sink_prior"] == ("3", 18)    # honest read, NOT the old fabricated 90


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


async def test_apply_ambient_cap_mirrors_mixer_raising_stranded_sink(monkeypatch):
    """The stranded-quiet bug: a track comes up at 18% (stream-restore replayed a duck onto the new
    sink-input). The mirror RAISES it to the mixer level — lower-only capping couldn't. The mixer here
    (70) is below the cap (90), so it must NOT be lowered, and the sink must mirror 70, not blast to 90."""
    calls = []
    async def fake_pactl(*a, **k):
        if a and a[0] == "list":
            return (0, 'Sink Input #4\n\tVolume: front-left: 11796 / 18% / -20 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    seen = []
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        seen.append((method, params)); return 70 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk.apply_ambient_cap(ctx)
    assert ("core.mixer.set_volume", {"volume": 90}) not in seen   # mixer below cap → left alone, no blast
    assert ("set-sink-input-volume", "4", "70%") in calls          # sink raised 18→70 to mirror the mixer


async def test_apply_ambient_cap_waits_for_late_sink_input(monkeypatch):
    """F1: a new track's PipeWire sink-input can appear a beat after play; if mirror-on-play runs before
    it exists, the track comes up stranded-quiet. apply_ambient_cap must poll for the sink, then mirror."""
    attempts = {"n": 0}
    async def fake_sink():
        attempts["n"] += 1
        return None if attempts["n"] < 3 else ("8", 18)   # appears (stranded at 18) on the 3rd poll
    calls = []
    async def fake_pactl(*a, **k):
        calls.append(a); return (0, "")
    async def no_sleep(_s):
        return None
    monkeypatch.setattr(_dk, "_mopidy_sink_input", fake_sink)
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    monkeypatch.setattr(_dk.asyncio, "sleep", no_sleep)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 50 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk.apply_ambient_cap(ctx)
    assert attempts["n"] == 3                                  # polled until the sink-input appeared
    assert ("set-sink-input-volume", "8", "50%") in calls      # then un-stranded it to the mixer level


async def test_apply_ambient_cap_skips_sink_during_active_duck(monkeypatch):
    """A speech-duck is in progress (sink intentionally at the duck level) → apply_ambient_cap must not
    raise the sink-input out from under the duck."""
    calls = []
    async def fake_pactl(*a, **k):
        if a and a[0] == "list":
            return (0, 'Sink Input #4\n\tVolume: front-left: 11796 / 18% / -20 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 70 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    _dk._state(ctx)["tidal_sink_prior"] = ("4", 70)               # a duck is active
    await _dk.apply_ambient_cap(ctx)
    assert not any(a and a[0] == "set-sink-input-volume" for a in calls)   # sink left at the duck level


async def test_apply_ambient_cap_reconciles_new_track_mid_duck(monkeypatch):
    """F1 residual: a new track starts WHILE a speech-duck is active (Rob rapid-changing songs). It gets a
    fresh sink-input (new index); left alone it strands at a stale level. apply_ambient_cap must duck the
    new sink to the active duck level and re-point the saved prior at it (keeping the prior so restore is
    unaffected) — NOT blanket-skip."""
    async def fake_sink():
        return ("9", 70)                                         # NEW track's sink-input (index changed)
    calls = []
    async def fake_pactl(*a, **k):
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_mopidy_sink_input", fake_sink)
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 50 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    _dk._state(ctx)["tidal_sink_prior"] = ("4", 70)              # duck active, saved at the OLD index
    _dk._state(ctx)["duck_window_open"] = True                   # the speech-duck window is genuinely open
    await _dk.apply_ambient_cap(ctx)
    assert ("set-sink-input-volume", "9", f"{_dk._SINK_DUCK_PCT}%") in calls   # new sink ducked, not stranded
    assert _dk._state(ctx)["tidal_sink_prior"] == ("9", 70)      # tuple re-pointed; prior preserved


async def test_apply_ambient_cap_reconcile_polls_for_late_new_sink(monkeypatch):
    """F1-residual race: the new track's sink-input appears a beat after tidal.play returns, so a single
    lookup finds the OLD sink and misses the reconcile. apply_ambient_cap must POLL for a new-index sink."""
    attempts = {"n": 0}
    async def fake_sink():
        attempts["n"] += 1
        return ("4", 0) if attempts["n"] < 3 else ("9", 70)   # old sink lingers, new ("9") appears 3rd poll
    calls = []
    async def fake_pactl(*a, **k):
        calls.append(a); return (0, "")
    async def no_sleep(_s):
        return None
    monkeypatch.setattr(_dk, "_mopidy_sink_input", fake_sink)
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    monkeypatch.setattr(_dk.asyncio, "sleep", no_sleep)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 50 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    _dk._state(ctx)["tidal_sink_prior"] = ("4", 70)             # duck active, on the OLD sink
    _dk._state(ctx)["duck_window_open"] = True                  # window genuinely open → duck the new sink
    await _dk.apply_ambient_cap(ctx)                            # new_track defaults True → polls
    assert attempts["n"] == 3                                   # polled until the new sink appeared
    assert ("set-sink-input-volume", "9", f"{_dk._SINK_DUCK_PCT}%") in calls
    assert _dk._state(ctx)["tidal_sink_prior"] == ("9", 70)     # re-pointed to the new sink


async def test_apply_ambient_cap_resume_skips_reconcile_poll(monkeypatch):
    """A resume-in-place (new_track=False) reuses the same sink, so apply_ambient_cap must NOT poll —
    single check, no reconcile, no added latency on a resume while a window is open."""
    attempts = {"n": 0}
    async def fake_sink():
        attempts["n"] += 1
        return ("4", 0)                                        # same ducked sink, never a new index
    calls = []
    async def fake_pactl(*a, **k):
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_mopidy_sink_input", fake_sink)
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 50 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    _dk._state(ctx)["tidal_sink_prior"] = ("4", 70)
    await _dk.apply_ambient_cap(ctx, new_track=False)          # resume → no poll
    assert attempts["n"] == 1                                   # single check only
    assert not any(a and a[0] == "set-sink-input-volume" for a in calls)  # nothing re-ducked


async def test_apply_ambient_cap_reconciles_new_track_to_zero_when_muted(monkeypatch):
    """A new mid-duck track during a MUTED window reconciles to 0%, not the partial duck level."""
    async def fake_sink():
        return ("9", 70)
    calls = []
    async def fake_pactl(*a, **k):
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_mopidy_sink_input", fake_sink)
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 50 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    _dk._state(ctx)["tidal_sink_prior"] = ("4", 70)
    _dk._state(ctx)["muted"] = True
    _dk._state(ctx)["duck_window_open"] = True
    await _dk.apply_ambient_cap(ctx)
    assert ("set-sink-input-volume", "9", "0%") in calls         # muted window → full 0, not the duck pct


async def test_apply_ambient_cap_reconcile_restores_when_window_closed(monkeypatch):
    """THE reconcile-vs-off race (the recurring 'I don't hear anything'): a new track is reconciled but the
    speech-duck window has CLOSED (stale prior lingered). Ducking it to 18% would strand it quiet with no off
    to follow. With the window flag False, the reconcile must RESTORE the new sink to the mixer level (50) and
    clear the stale prior — not leave it at the duck level."""
    async def fake_sink():
        return ("9", 18)                                         # new track's sink, came up stranded-quiet
    calls = []
    async def fake_pactl(*a, **k):
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_mopidy_sink_input", fake_sink)
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    import gabagent.commands.providers.tidal as td
    async def fake_rpc(tc, method, params=None, timeout=2.0):
        return 50 if method == "core.mixer.get_volume" else None
    monkeypatch.setattr(td, "_rpc", fake_rpc)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    _dk._state(ctx)["tidal_sink_prior"] = ("4", 50)             # stale prior left by the race
    _dk._state(ctx)["duck_window_open"] = False                 # ...but the window has actually closed
    await _dk.apply_ambient_cap(ctx)
    assert ("set-sink-input-volume", "9", "50%") in calls        # restored to the real level, NOT 18
    assert ("set-sink-input-mute", "9", "0") in calls            # and unmuted (stray-mute guard)
    assert _dk._state(ctx)["tidal_sink_prior"] is None           # stale prior cleared


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


# -- mute mode: deepen the duck to a full mute (vol 0) while the wake window is open ----------------

@respx.mock
async def test_duck_tidal_mute_from_idle():
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    out = await duck_media(ctx, True, mute=True)
    assert out["ducked"] == ["tidal"]
    assert ("core.mixer.set_volume", {"volume": 0}) in seen      # full mute, not the gentle 18
    assert ("core.mixer.set_volume", {"volume": 18}) not in seen
    assert _dk._state(ctx)["tidal_prior"] == 80                  # saved the REAL level, not 0
    assert _dk._state(ctx)["muted"] is True


@respx.mock
async def test_duck_tidal_mute_deepens_existing_duck():
    """VAD onset ducks to 18; the wake window then sends mute=True → deepen to 0 WITHOUT re-reading or
    clobbering the saved 80 (so restore still returns to the real level)."""
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    await duck_media(ctx, True)                                  # plain duck → 18, save 80
    seen.clear()
    await duck_media(ctx, True, mute=True)                       # deepen → 0
    assert ("core.mixer.set_volume", {"volume": 0}) in seen
    assert not any(m == "core.mixer.get_volume" for m, _ in seen)  # prior NOT re-read/clobbered
    assert _dk._state(ctx)["tidal_prior"] == 80                  # still the original real level
    assert _dk._state(ctx)["muted"] is True


@respx.mock
async def test_duck_tidal_mute_restores_prior():
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    await duck_media(ctx, True, mute=True)                       # save 80, mute to 0
    seen.clear()
    await duck_media(ctx, False)                                 # restore
    assert ("core.mixer.set_volume", {"volume": 80}) in seen     # back to the real level
    assert _dk._state(ctx)["muted"] is False
    assert _dk._state(ctx)["tidal_prior"] is None


@respx.mock
async def test_duck_tidal_mute_then_mute_is_noop():
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    await duck_media(ctx, True, mute=True)                       # mute → 0, muted True
    seen.clear()
    out = await duck_media(ctx, True, mute=True)                 # already muted → clean no-op
    assert out["ducked"] == [] and not seen


@respx.mock
async def test_duck_non_mute_unchanged_regression():
    """A plain duck (no mute flag) is byte-for-byte the old behavior: 18, never 0, muted stays False."""
    ctx = _ctx(tidal=True, jellyfin=False)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    await duck_media(ctx, True)
    assert ("core.mixer.set_volume", {"volume": 18}) in seen
    assert not any(p == {"volume": 0} for m, p in seen if m == "core.mixer.set_volume")
    assert _dk._state(ctx)["muted"] is False


@respx.mock
async def test_duck_jellyfin_browser_mute():
    ctx = _ctx(tidal=False, jellyfin=True)
    page = _FakeVideoPage(volume=0.9); ctx.jellyfin_playing_page = page
    assert (await duck_media(ctx, True, mute=True))["ducked"] == ["jellyfin"]
    assert page.volume == 0.0                                   # full mute, not the gentle 0.2
    assert _dk._state(ctx)["jellyfin_video_volume"] == 0.9      # saved the REAL prior
    assert _dk._state(ctx)["muted"] is True
    assert (await duck_media(ctx, False))["ducked"] == ["jellyfin"]
    assert page.volume == 0.9                                   # restored the exact prior
    assert _dk._state(ctx)["muted"] is False


@respx.mock
async def test_duck_jellyfin_browser_mute_deepens():
    ctx = _ctx(tidal=False, jellyfin=True)
    page = _FakeVideoPage(volume=0.9); ctx.jellyfin_playing_page = page
    await duck_media(ctx, True)                                  # plain duck → 0.2, save 0.9
    assert page.volume == 0.2
    await duck_media(ctx, True, mute=True)                       # deepen → 0.0, prior preserved
    assert page.volume == 0.0
    assert _dk._state(ctx)["jellyfin_video_volume"] == 0.9
    assert _dk._state(ctx)["muted"] is True
    await duck_media(ctx, False)
    assert page.volume == 0.9                                   # restored to the real level


async def test_duck_tidal_sink_mute(monkeypatch):
    """The PipeWire sink-input belt mutes to 0% and preserves the saved prior tuple for restore."""
    calls = []
    async def fake_pactl(*a, **k):
        if a and a[0] == "list":
            return (0, 'Sink Input #7\n\tVolume: front-left: 52000 / 80% / -5 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        calls.append(a); return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t", media_ambient_cap=90))
    await _dk._duck_tidal_sink(ctx, True, mute=True)
    assert ("set-sink-input-volume", "7", "0%") in calls
    assert _dk._state(ctx)["tidal_sink_prior"] == ("7", 80)     # real prior saved, not 0
    calls.clear()
    await _dk._duck_tidal_sink(ctx, False)
    assert ("set-sink-input-volume", "7", "80%") in calls       # restored to the prior


# -- set_volume vs an active duck: the "turn it down → blasts back up" fix (option b) --------------

@respx.mock
async def test_duck_then_set_volume_restores_to_new_level(monkeypatch):
    """The reported repro: a volume set issued WHILE ducked (command window open) updates the restore
    prior and does NOT touch the live output (option b), so duck-off returns to the user's NEW level —
    not the stale pre-duck level that used to blast back."""
    from gabagent.commands.providers.tidal import set_volume
    ctx = _ctx(tidal=True)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    sink_calls = []
    async def fake_pactl(*args, **k):
        if args and args[0] == "list":
            return (0, 'Sink Input #5\n\tVolume: front-left: 52000 / 80% / -1 dB\n'
                       '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
        sink_calls.append(args)
        return (0, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)

    await duck_media(ctx, True)                        # saves prior 80, ducks mixer+sink to 18
    assert _dk._state(ctx)["tidal_prior"] == 80
    seen.clear(); sink_calls.clear()

    res = await set_volume(ctx, level=30)              # user lowers it while the window is open
    assert "30 percent" in res.output
    assert ("core.mixer.set_volume", {"volume": 30}) not in seen          # option b: no live write mid-window
    assert not any(a and a[0] == "set-sink-input-volume" for a in sink_calls)
    assert _dk._state(ctx)["tidal_prior"] == 30                           # prior updated to the new level
    assert _dk._state(ctx)["tidal_sink_prior"] == ("5", 30)

    seen.clear()
    await duck_media(ctx, False)                       # restore honors the NEW level
    assert ("core.mixer.set_volume", {"volume": 30}) in seen              # restored to 30, not stale 80
    assert ("core.mixer.set_volume", {"volume": 80}) not in seen


@respx.mock
async def test_set_volume_writes_live_when_no_duck():
    """No active duck → set_volume writes the live mixer as before; note_user_volume returns False."""
    from gabagent.commands.providers.tidal import set_volume
    ctx = _ctx(tidal=True)
    seen = []
    respx.post(RPC).mock(side_effect=_mopidy(seen, vol=80))
    assert _dk.note_user_volume(ctx, 40) is False                         # nothing ducked
    res = await set_volume(ctx, level=40)
    assert ("core.mixer.set_volume", {"volume": 40}) in seen              # live write happened
    assert "40 percent" in res.output


async def test_set_volume_updates_sink_prior_during_duck(monkeypatch):
    """A set_volume during an active duck updates BOTH saved priors and writes NO live output."""
    from gabagent.commands.providers.tidal import set_volume
    ctx = _ctx(tidal=True)
    st = _dk._state(ctx)
    st["tidal_prior"] = 80
    st["tidal_sink_prior"] = ("5", 80)
    sink_calls = []
    async def fake_pactl(*args, **k):
        sink_calls.append(args)
        return (1, "")
    monkeypatch.setattr(_dk, "_run_pactl", fake_pactl)
    res = await set_volume(ctx, level=30)
    assert st["tidal_prior"] == 30
    assert st["tidal_sink_prior"] == ("5", 30)
    assert not any(a and a[0] == "set-sink-input-volume" for a in sink_calls)   # no live sink write
    assert "30 percent" in res.output


# -- universal local-media duck: heard over ANY local media on a full-mute window-open -------------

def _fake_pactl(listing, sets):
    async def fake(*args, **k):
        if args and args[0] == "list":
            return (0, listing)
        if args and args[0] == "set-sink-input-volume":
            sets.append((args[1], args[2]))
        return (0, "")
    return fake


async def test_universal_duck_mutes_unowned_local_then_restores(monkeypatch):
    listing = ('Sink Input #10\n\tVolume: front-left: 52000 / 80% / -5 dB\n'
               '\tProperties:\n\t\tapplication.name = "Chromium"\n')
    sets = []
    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl(listing, sets))
    monkeypatch.setattr(_dk.shutil, "which", lambda _n: "/usr/bin/pactl")
    ctx = _ctx(tidal=False, jellyfin=False)
    out = await duck_media(ctx, True, mute=True)
    assert "local" in out["ducked"]
    assert ("10", "0%") in sets                                  # unowned stream muted to 0
    assert _dk._state(ctx)["local_sink_priors"] == {"10": 80}    # prior saved
    sets.clear()
    out2 = await duck_media(ctx, False)
    assert "local" in out2["ducked"]
    assert ("10", "80%") in sets                                 # restored to the exact prior
    assert _dk._state(ctx)["local_sink_priors"] is None


async def test_universal_duck_excludes_tts_and_mopidy(monkeypatch):
    listing = ('Sink Input #10\n\tVolume: front-left: 52000 / 80% / -5 dB\n'
               '\tProperties:\n\t\tapplication.name = "Chromium"\n'
               'Sink Input #11\n\tVolume: front-left: 45000 / 70% / -8 dB\n'
               '\tProperties:\n\t\tnode.name = "alsa_playback.python3.12"\n'
               'Sink Input #12\n\tVolume: front-left: 60000 / 90% / -3 dB\n'
               '\tProperties:\n\t\tapplication.name = "Mopidy"\n')
    sets = []
    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl(listing, sets))
    monkeypatch.setattr(_dk.shutil, "which", lambda _n: "x")
    ctx = _ctx(tidal=False, jellyfin=False)
    await duck_media(ctx, True, mute=True)
    assert ("10", "0%") in sets                                  # unowned Chromium ducked
    assert not any(idx == "11" for idx, _ in sets)               # TTS (alsa_playback) NEVER muted
    assert not any(idx == "12" for idx, _ in sets)               # Mopidy (owned) handled elsewhere
    assert _dk._state(ctx)["local_sink_priors"] == {"10": 80}


async def test_universal_duck_excludes_stamped_property(monkeypatch):
    listing = ('Sink Input #20\n\tVolume: front-left: 52000 / 55% / -5 dB\n'
               '\tProperties:\n\t\tapplication.name = "SomeApp"\n'
               '\t\tgabagent.duck_exclude = "1"\n')
    sets = []
    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl(listing, sets))
    monkeypatch.setattr(_dk.shutil, "which", lambda _n: "x")
    ctx = _ctx(tidal=False, jellyfin=False)
    out = await duck_media(ctx, True, mute=True)
    assert "local" not in out["ducked"] and sets == []          # stamped stream excluded → nothing ducked


async def test_universal_duck_skips_plain_vad_duck(monkeypatch):
    listing = ('Sink Input #30\n\tVolume: front-left: 52000 / 80% / -5 dB\n'
               '\tProperties:\n\t\tapplication.name = "Chromium"\n')
    sets = []
    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl(listing, sets))
    monkeypatch.setattr(_dk.shutil, "which", lambda _n: "x")
    ctx = _ctx(tidal=False, jellyfin=False)
    out = await duck_media(ctx, True, mute=False)               # gentle VAD-onset duck, not a full mute
    assert "local" not in out["ducked"] and sets == []          # unowned media left alone until window-open
    assert _dk._state(ctx)["local_sink_priors"] is None
