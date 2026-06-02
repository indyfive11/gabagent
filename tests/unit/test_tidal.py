import json
import types
import httpx
import respx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import tidal as td

RPC = "http://mopidy.test:6680/mopidy/rpc"

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
