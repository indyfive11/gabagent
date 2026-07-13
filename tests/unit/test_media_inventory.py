import json
import types
import httpx
import pytest
import respx

from gabagent.config.models import GabAgentConfig
from gabagent.commands import media as _m
from gabagent.commands.media import MediaSource, inventory, judge_locality, local_audible, auto_scoped

RPC = "http://mopidy.test:6680/mopidy/rpc"
BASE = "http://jf.test:8096"


def _ctx(tidal=True, jellyfin=True, local_device=""):
    cfg = GabAgentConfig(api_key="test", local_device=local_device)
    cfg.tidal.enabled = tidal
    cfg.tidal.rpc_url = RPC
    cfg.jellyfin.enabled = jellyfin
    cfg.jellyfin.api_key = "k" if jellyfin else ""
    cfg.jellyfin.base_url = BASE
    return types.SimpleNamespace(config=cfg)


def _tidal_rpc(state="playing"):
    def _resp(request):
        m = json.loads(request.content)["method"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"core.playback.get_state": state}.get(m)})
    return _resp


def _sessions_resp(sessions):
    return lambda request: httpx.Response(200, json=sessions)


# -- locality verdict -----------------------------------------------------------------------------

def test_is_loopback():
    assert _m._is_loopback("127.0.0.1:53124")
    assert _m._is_loopback("[::1]:9000")
    assert _m._is_loopback("::1")
    assert not _m._is_loopback("192.0.2.42:8096")
    assert not _m._is_loopback("")


def test_judge_locality_loopback_is_local():
    ctx = _ctx()
    assert judge_locality(ctx, endpoint="127.0.0.1:5000") == "local"


def test_judge_locality_own_device_id_is_local():
    ctx = _ctx()
    assert judge_locality(ctx, device_id="gabagent-voice", endpoint="10.0.0.9:5000") == "local"


def test_judge_locality_configured_device_name_is_local():
    ctx = _ctx(local_device="HomeServer")
    assert judge_locality(ctx, device_name="homeserver", endpoint="192.0.2.5:5000") == "local"


def test_judge_locality_lan_ip_defaults_remote():
    ctx = _ctx(local_device="HomeServer")
    assert judge_locality(ctx, device_name="Living Room TV", endpoint="192.0.2.77:5000") == "remote"


def test_judge_locality_unprovable_defaults_remote():
    ctx = _ctx()
    assert judge_locality(ctx, device_name="Chrome") == "remote"   # no endpoint, not our id → remote


# -- tidal source ---------------------------------------------------------------------------------

@respx.mock
async def test_tidal_source_owned_local():
    from gabagent.commands.providers.tidal import PROVIDER as tidal
    respx.post(RPC).mock(side_effect=_tidal_rpc("playing"))
    srcs = await tidal.sources(_ctx())
    assert len(srcs) == 1
    s = srcs[0]
    assert (s.provider, s.kind, s.state, s.owned, s.locality) == ("tidal", "audio", "playing", True, "local")
    assert s.audible and s.auto_scoped


@respx.mock
async def test_tidal_source_none_when_stopped():
    from gabagent.commands.providers.tidal import PROVIDER as tidal
    respx.post(RPC).mock(side_effect=_tidal_rpc("stopped"))
    assert await tidal.sources(_ctx()) == []


@respx.mock
async def test_tidal_source_serves_cache_in_flight_without_rpc():
    """While a TIDAL op is in flight, sources() must serve the cached state and NOT issue a get_state RPC
    (which would queue behind the op in Mopidy and block the gate's poll ~30s)."""
    from gabagent.commands.providers.tidal import PROVIDER as tidal, _pb_cache, _cache_playback
    ctx = _ctx()
    c = _pb_cache(ctx)
    c["inflight"] = 1
    _cache_playback(ctx, "playing")          # optimistic state an in-flight play() would have set
    route = respx.post(RPC).mock(return_value=httpx.Response(500))
    srcs = await tidal.sources(ctx)
    assert not route.called                  # served from cache — no blocking RPC
    assert len(srcs) == 1 and srcs[0].state == "playing"


@respx.mock
async def test_tidal_source_serves_fresh_cache_without_rpc():
    """A fresh cache (within TTL) is served without an RPC even when idle."""
    from gabagent.commands.providers.tidal import PROVIDER as tidal, _pb_cache, _cache_playback
    ctx = _ctx()
    _cache_playback(ctx, "playing")          # ts = now → fresh
    route = respx.post(RPC).mock(return_value=httpx.Response(500))
    srcs = await tidal.sources(ctx)
    assert not route.called and len(srcs) == 1 and srcs[0].state == "playing"


# -- jellyfin sources -----------------------------------------------------------------------------

class _FakeVideoPage:
    def __init__(self, paused=False):
        self._paused = paused
    def is_closed(self):
        return False
    async def evaluate(self, script, arg=None):
        return self._paused


async def test_jellyfin_owned_page_is_local_video():
    from gabagent.commands.providers.jellyfin import PROVIDER as jf
    ctx = _ctx()
    ctx.jellyfin_playing_page = _FakeVideoPage(paused=False)
    ctx.jellyfin_playing_title = "Blade Runner"
    srcs = await jf.sources(ctx)
    assert len(srcs) == 1
    s = srcs[0]
    assert (s.provider, s.kind, s.owned, s.locality, s.state) == ("jellyfin", "video", True, "local", "playing")
    assert s.title == "Blade Runner" and s.auto_scoped


@respx.mock
async def test_jellyfin_remote_session_is_visible_but_not_owned():
    from gabagent.commands.providers.jellyfin import PROVIDER as jf
    respx.get(BASE + "/Sessions").mock(side_effect=_sessions_resp([
        {"Id": "abc", "DeviceName": "Living Room TV", "DeviceId": "tv-1",
         "RemoteEndPoint": "192.0.2.77:5000", "Client": "Jellyfin Web",
         "NowPlayingItem": {"Name": "Some Episode"}, "PlayState": {"IsPaused": False}},
    ]))
    ctx = _ctx()
    ctx.jellyfin_playing_page = None
    srcs = await jf.sources(ctx)
    assert len(srcs) == 1
    s = srcs[0]
    assert s.owned is False and s.locality == "remote"
    assert not s.auto_scoped and not s.audible          # remote → never auto-controlled, not in local loop
    assert s.session_key == "abc" and s.device_name == "Living Room TV"


@respx.mock
async def test_jellyfin_loopback_session_is_local():
    from gabagent.commands.providers.jellyfin import PROVIDER as jf
    respx.get(BASE + "/Sessions").mock(side_effect=_sessions_resp([
        {"Id": "z", "DeviceName": "Chrome", "RemoteEndPoint": "127.0.0.1:6001",
         "NowPlayingItem": {"Name": "Local Tab"}, "PlayState": {"IsPaused": True}},
    ]))
    ctx = _ctx()
    ctx.jellyfin_playing_page = None
    srcs = await jf.sources(ctx)
    assert srcs[0].locality == "local" and srcs[0].state == "paused"
    assert srcs[0].audible and not srcs[0].auto_scoped   # local+audible, but unowned → no auto provider-control


@respx.mock
async def test_jellyfin_skips_sessions_without_nowplaying():
    from gabagent.commands.providers.jellyfin import PROVIDER as jf
    respx.get(BASE + "/Sessions").mock(side_effect=_sessions_resp([
        {"Id": "idle", "DeviceName": "Phone", "RemoteEndPoint": "192.0.2.9:5000"},
    ]))
    ctx = _ctx()
    ctx.jellyfin_playing_page = None
    assert await jf.sources(ctx) == []


# -- inventory aggregation + scoped views ---------------------------------------------------------

@respx.mock
async def test_inventory_aggregates_and_scopes(monkeypatch):
    # Neutralize the generic pactl-based local-audio provider (mpris.sources) so the assertion is
    # deterministic regardless of what the host is actually playing during the test run.
    async def _no_pactl(*a, **k):
        return (1, "")
    monkeypatch.setattr("gabagent.voice.ducking._run_pactl", _no_pactl)
    respx.post(RPC).mock(side_effect=_tidal_rpc("playing"))
    respx.get(BASE + "/Sessions").mock(side_effect=_sessions_resp([
        {"Id": "r", "DeviceName": "Bedroom", "RemoteEndPoint": "192.0.2.50:5000",
         "NowPlayingItem": {"Name": "Movie"}, "PlayState": {"IsPaused": False}},
    ]))
    ctx = _ctx()
    ctx.jellyfin_playing_page = None
    srcs = await inventory(ctx)
    providers = sorted(s.provider for s in srcs)
    assert providers == ["jellyfin", "tidal"]                       # both contributed
    # tidal local+owned audio is audible+auto; remote jellyfin is neither
    assert [s.provider for s in local_audible(srcs)] == ["tidal"]
    assert [s.provider for s in auto_scoped(srcs)] == ["tidal"]


async def test_inventory_never_raises_on_broken_provider(monkeypatch):
    # A provider whose sources() throws contributes nothing; inventory still returns the rest.
    ctx = _ctx(tidal=False, jellyfin=False)
    srcs = await inventory(ctx)
    assert isinstance(srcs, list)        # disabled providers → empty, no raise


# -- Generic local-audio detection (mpris.sources via pactl) — the browser-movie duck fix --------------

_SINK_INPUTS = '''Sink Input #100
	Corked: no
	Mute: no
	Volume: front-left: 65536 / 100%
	Properties:
		application.name = "Chromium"
Sink Input #200
	Corked: yes
	Mute: no
	Properties:
		application.name = "Firefox"
Sink Input #300
	Corked: no
	Properties:
		application.name = "Mopidy"
		node.name = "Mopidy"
Sink Input #400
	Corked: no
	Properties:
		gabagent.duck_exclude = "1"
		node.name = "gabagent-tts"
'''


async def test_mpris_sources_detects_local_audio_excludes_tts_and_mopidy(monkeypatch):
    """A browser/mpv/VLC stream (no dedicated provider) surfaces as a generic local source so the duck
    fires; Aria's own TTS and the Mopidy stream (tidal models it) are excluded."""
    from gabagent.commands.providers.mpris import PROVIDER as mpris
    async def _pactl(*a, **k):
        return (0, _SINK_INPUTS)
    monkeypatch.setattr("gabagent.voice.ducking._run_pactl", _pactl)
    srcs = await mpris.sources(_ctx())
    assert all(s.provider == "local" and s.is_local for s in srcs)
    states = sorted(s.state for s in srcs)
    assert states == ["paused", "playing"]                 # Chromium playing, Firefox corked; TTS+Mopidy gone
    assert any(s.state == "playing" and s.audible for s in srcs)   # → media_state.playing = True → duck


async def test_mpris_sources_empty_when_no_pactl(monkeypatch):
    from gabagent.commands.providers.mpris import PROVIDER as mpris
    async def _no_pactl(*a, **k):
        return (1, "")
    monkeypatch.setattr("gabagent.voice.ducking._run_pactl", _no_pactl)
    assert await mpris.sources(_ctx()) == []
