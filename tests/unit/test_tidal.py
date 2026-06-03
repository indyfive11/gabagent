import json
import types
import httpx
import respx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import tidal as td

RPC = "http://mopidy.test:6680/mopidy/rpc"


@pytest.fixture(autouse=True)
def _no_ambient_cap(monkeypatch):
    """Don't let play's ambient-cap hook touch real system audio (pactl) or perturb the asserted RPC
    sequence — stub it. The ambient cap's own behavior is covered in test_ducking."""
    async def _noop(ctx):
        return None
    monkeypatch.setattr("gabagent.voice.ducking.apply_ambient_cap", _noop)

_TRACK = {
    "__model__": "Track", "uri": "tidal:track:1", "name": "So What",
    "artists": [{"name": "Miles Davis"}], "album": {"name": "Kind of Blue"}, "length": 540000,
}


def _ctx(**tcfg):
    cfg = GabAgentConfig(api_key="test")
    cfg.tidal.rpc_url = RPC
    for k, v in tcfg.items():
        setattr(cfg.tidal, k, v)
    return types.SimpleNamespace(config=cfg, voice_session=None, voice_emit=None)


def _rpc_router(handlers):
    """respx side_effect: dispatch by the JSON-RPC method in the request body."""
    def _resp(request):
        method = json.loads(request.content)["method"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": handlers.get(method)})
    return _resp


@respx.mock
async def test_detect_true_when_reachable():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.get_version": "3.4.2"}))
    assert await td.PROVIDER.detect(_ctx()) is True


async def test_detect_false_when_unreachable():
    ctx = _ctx(rpc_url="http://127.0.0.1:1/mopidy/rpc")
    assert await td.PROVIDER.detect(ctx) is False


def test_commands_are_tier1_media():
    cmds = {c.id: c for c in td.PROVIDER.commands(_ctx())}
    assert set(cmds) >= {"tidal.search", "tidal.play", "tidal.pause", "tidal.next", "tidal.now_playing"}
    assert all(c.tier == 1 and c.domain == "media" for c in cmds.values())
    assert cmds["tidal.search"].structured and cmds["tidal.now_playing"].structured


@respx.mock
async def test_search_returns_structured_tracks():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.library.search": [{"tracks": [_TRACK]}]}))
    res = await td.search(_ctx(), query="miles davis")
    assert res.success
    data = json.loads(res.output)
    assert data[0]["uri"] == "tidal:track:1"
    assert data[0]["title"] == "So What" and data[0]["artist"] == "Miles Davis"


@respx.mock
async def test_play_searches_then_queues_and_plays():
    seen = []

    def _resp(request):
        method = json.loads(request.content)["method"]
        seen.append(method)
        result = [{"tracks": [_TRACK]}] if method == "core.library.search" else None
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    respx.post(RPC).mock(side_effect=_resp)
    res = await td.play(_ctx(), query="kind of blue")
    assert res.success and "So What" in res.output and "Miles Davis" in res.output
    # the full search -> clear -> add -> play sequence ran, in order
    assert seen == ["core.library.search", "core.tracklist.clear",
                    "core.tracklist.add", "core.playback.play"]


@respx.mock
async def test_play_no_args_resumes():
    seen = []

    def _resp(request):
        seen.append(json.loads(request.content)["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})

    respx.post(RPC).mock(side_effect=_resp)
    res = await td.play(_ctx())
    assert res.success and seen == ["core.playback.resume"]


@respx.mock
async def test_play_not_found():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.library.search": [{"tracks": []}]}))
    res = await td.play(_ctx(), query="nonexistent zzz")
    assert not res.success and "couldn't find" in res.error.lower()


@respx.mock
async def test_transport_and_now_playing():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.playback.get_current_track": _TRACK}))
    assert (await td.pause(_ctx())).output == "Paused."
    assert (await td.next(_ctx())).output == "Skipping ahead."
    np = json.loads((await td.now_playing(_ctx())).output)
    assert np["playing"] and np["title"] == "So What" and np["artist"] == "Miles Davis"


@respx.mock
async def test_now_playing_nothing():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.playback.get_current_track": None}))
    np = json.loads((await td.now_playing(_ctx())).output)
    assert np == {"playing": False}


@respx.mock
async def test_rpc_error_surfaces():
    respx.post(RPC).mock(return_value=httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "boom"}}))
    res = await td.search(_ctx(), query="x")
    assert not res.success and "boom" in res.error


@respx.mock
async def test_play_album_searches_expands_and_queues_all():
    seen = []

    def resp(request):
        body = json.loads(request.content); m = body["method"]; seen.append((m, body.get("params")))
        result = {
            "core.library.search": [{"albums": [
                {"uri": "tidal:album:1", "name": "Dizzy Up the Girl", "artists": [{"name": "Goo Goo Dolls"}]}]}],
            "core.library.lookup": {"tidal:album:1": [{"uri": "tidal:track:1"}, {"uri": "tidal:track:2"}]},
        }.get(m)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    respx.post(RPC).mock(side_effect=resp)
    res = await td.play(_ctx(), query="Dizzy Up the Girl", album=True)
    assert res.success and "album" in res.output.lower() and "Dizzy Up the Girl" in res.output
    methods = [m for m, _ in seen]
    assert methods == ["core.library.search", "core.library.lookup",
                       "core.tracklist.clear", "core.tracklist.add", "core.playback.play"]
    assert ("core.tracklist.add", {"uris": ["tidal:track:1", "tidal:track:2"]}) in seen


@respx.mock
async def test_play_album_uri_skips_search():
    seen = []

    def resp(request):
        m = json.loads(request.content)["method"]; seen.append(m)
        result = {"core.library.lookup": {"tidal:album:9": [{"uri": "tidal:track:9"}]}}.get(m)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    respx.post(RPC).mock(side_effect=resp)
    res = await td.play(_ctx(), uri="tidal:album:9")     # album uri → album path, no flag needed
    assert res.success
    assert "core.library.search" not in seen and "core.library.lookup" in seen


@respx.mock
async def test_search_includes_albums():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.library.search": [
        {"tracks": [_TRACK], "albums": [{"uri": "tidal:album:1", "name": "Kind of Blue",
                                         "artists": [{"name": "Miles Davis"}]}]}]}))
    data = json.loads((await td.search(_ctx(), query="kind of blue")).output)
    kinds = {d["kind"] for d in data}
    assert kinds == {"track", "album"}


# -- playlists + recommendations + container playback ----------------------

def test_new_commands_registered_and_featured():
    cmds = {c.id: c for c in td.PROVIDER.commands(_ctx())}
    assert {"tidal.playlists", "tidal.recommendations"} <= set(cmds)
    assert cmds["tidal.playlists"].featured and cmds["tidal.recommendations"].featured
    assert cmds["tidal.playlists"].structured and cmds["tidal.recommendations"].structured


def test_set_volume_registered_featured_and_typed():
    cmds = {c.id: c for c in td.PROVIDER.commands(_ctx())}
    assert "tidal.set_volume" in cmds and cmds["tidal.set_volume"].featured
    lvl = next(s for s in cmds["tidal.set_volume"].params if s.name == "level")
    assert lvl.type == "integer" and lvl.required


@respx.mock
async def test_set_volume_targets_stream_not_master_sink(monkeypatch):
    """The absolute set must hit the Mopidy mixer AND the Mopidy sink-input — never the master sink
    (@DEFAULT_SINK@), which would mute the assistant's own voice."""
    seen = []
    respx.post(RPC).mock(side_effect=lambda r: (seen.append(json.loads(r.content)["method"]),
                         httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None}))[1])
    pactl_args = []
    async def _fake_sink():
        return ("677", 100)
    async def _fake_pactl(*args, **kw):
        pactl_args.append(args)
        return (0, "")
    monkeypatch.setattr("gabagent.voice.ducking._mopidy_sink_input", _fake_sink)
    monkeypatch.setattr("gabagent.voice.ducking._run_pactl", _fake_pactl)

    res = await td.set_volume(_ctx(), level=30)
    assert res.success and "30 percent" in res.output
    assert "core.mixer.set_volume" in seen                       # software mixer (the stream)
    assert pactl_args == [("set-sink-input-volume", "677", "30%")]  # the stream's node, by index
    assert all("@DEFAULT_SINK@" not in " ".join(a) for a in pactl_args)  # never the master sink


@respx.mock
async def test_set_volume_floor_clamps_and_validates(monkeypatch):
    seen_vol = []
    def _resp(r):
        body = json.loads(r.content)
        if body["method"] == "core.mixer.set_volume":
            seen_vol.append(body["params"]["volume"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})
    respx.post(RPC).mock(side_effect=_resp)
    async def _none():
        return None
    monkeypatch.setattr("gabagent.voice.ducking._mopidy_sink_input", _none)
    ctx = _ctx()
    ctx.config.media_volume_floor = 5
    # 0% is clamped up to the floor so music can't be ratcheted to silence
    await td.set_volume(ctx, level=0)
    assert seen_vol[-1] == 5
    # a non-numeric level is a clean error, not a crash
    bad = await td.set_volume(ctx, level="loud")
    assert not bad.success and "number" in bad.error


@respx.mock
async def test_playlists_lists_saved_playlists():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.playlists.as_list": [
        {"uri": "tidal:playlist:a", "name": "Metallica", "type": "playlist"},
        {"uri": "tidal:playlist:b", "name": "Folk ", "type": "playlist"},
    ]}))
    data = json.loads((await td.playlists(_ctx())).output)
    assert data == [{"kind": "playlist", "uri": "tidal:playlist:a", "title": "Metallica"},
                    {"kind": "playlist", "uri": "tidal:playlist:b", "title": "Folk"}]


@respx.mock
async def test_recommendations_prefers_personal_mixes():
    respx.post(RPC).mock(side_effect=_rpc_router({"core.library.browse": [
        {"uri": "tidal:mix:1", "name": "My Mix 3", "type": "playlist"},
        {"uri": "tidal:mix:2", "name": "My New Arrivals", "type": "playlist"},
    ]}))
    data = json.loads((await td.recommendations(_ctx())).output)
    assert [d["title"] for d in data] == ["My Mix 3", "My New Arrivals"]
    assert all(d["kind"] == "mix" for d in data)


@respx.mock
async def test_recommendations_empty_when_nothing_exposed():
    # browse returns nothing for every node → graceful "[]" (not an error)
    respx.post(RPC).mock(side_effect=_rpc_router({"core.library.browse": []}))
    res = await td.recommendations(_ctx())
    assert res.success and res.output == "[]"


@respx.mock
async def test_play_playlist_uri_expands_via_get_items():
    seen = []

    def resp(request):
        body = json.loads(request.content); m = body["method"]; seen.append((m, body.get("params")))
        result = {"core.playlists.get_items": [{"uri": "tidal:track:1"}, {"uri": "tidal:track:2"}]}.get(m)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    respx.post(RPC).mock(side_effect=resp)
    res = await td.play(_ctx(), uri="tidal:playlist:a")
    assert res.success and "playlist" in res.output.lower()
    methods = [m for m, _ in seen]
    assert methods == ["core.playlists.get_items", "core.tracklist.clear",
                       "core.tracklist.add", "core.playback.play"]
    assert ("core.tracklist.add", {"uris": ["tidal:track:1", "tidal:track:2"]}) in seen


@respx.mock
async def test_play_mix_uri_expands_via_browse():
    seen = []

    def resp(request):
        body = json.loads(request.content); m = body["method"]; seen.append(m)
        result = {"core.library.browse": [
            {"uri": "tidal:track:7", "type": "track"},
            {"uri": "tidal:track:8", "type": "track"},
            {"uri": "tidal:directory:x", "type": "directory"},  # non-track refs are dropped
        ]}.get(m)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    respx.post(RPC).mock(side_effect=resp)
    res = await td.play(_ctx(), uri="tidal:mix:1")
    assert res.success and "mix" in res.output.lower()
    assert seen == ["core.library.browse", "core.tracklist.clear",
                    "core.tracklist.add", "core.playback.play"]


async def test_expand_playable_dispatches_by_prefix():
    assert td._is_container_uri("tidal:album:1")
    assert td._is_container_uri("tidal:playlist:1")
    assert td._is_container_uri("tidal:mix:1")
    assert not td._is_container_uri("tidal:track:1")
    assert td._container_noun("tidal:playlist:1") == "playlist"
    assert td._container_noun("tidal:mix:1") == "mix"
    assert td._container_noun("tidal:album:1") == "album"


# -- timeouts: speakable errors + one retry -------------------------------

import asyncio  # noqa: E402


def test_human_err_is_always_speakable():
    assert td._human_err(httpx.ReadTimeout("x")) == "it timed out"     # timeout type
    assert td._human_err(asyncio.TimeoutError()) == "it timed out"
    assert td._human_err(RuntimeError("")) == "it timed out"           # empty message → not a bare colon
    assert td._human_err(RuntimeError("boom")) == "boom"               # real message passes through


@respx.mock
async def test_search_timeout_yields_speakable_error():
    respx.post(RPC).mock(side_effect=httpx.ReadTimeout(""))
    res = await td.search(_ctx(), query="x")
    assert not res.success
    assert res.error == "TIDAL search failed: it timed out"            # no dangling colon


@respx.mock
async def test_rpc_resilient_retries_once_on_timeout_then_succeeds():
    calls = {"n": 0}

    def _resp(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [{"tracks": [_TRACK]}]})

    respx.post(RPC).mock(side_effect=_resp)
    res = await td.search(_ctx(), query="miles")
    assert res.success and json.loads(res.output)[0]["uri"] == "tidal:track:1"
    assert calls["n"] == 2     # timed out once, retried, succeeded
