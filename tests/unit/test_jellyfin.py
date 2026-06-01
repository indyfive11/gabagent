import json
import types
import httpx
import respx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import jellyfin as jf
from gabagent.voice.session import VoiceSession

BASE = "http://jf.test:8096"


def _ctx(**jcfg):
    cfg = GabAgentConfig(api_key="test")
    cfg.jellyfin.base_url = BASE
    cfg.jellyfin.api_key = "k"
    for k, v in jcfg.items():
        setattr(cfg.jellyfin, k, v)
    return types.SimpleNamespace(config=cfg, voice_session=None, voice_emit=None)


@respx.mock
async def test_detect_true_when_reachable():
    respx.get(f"{BASE}/System/Info/Public").mock(return_value=httpx.Response(200, json={"Version": "10.12"}))
    assert await jf.PROVIDER.detect(_ctx()) is True


async def test_detect_false_when_unreachable():
    ctx = _ctx()
    ctx.config.jellyfin.base_url = "http://127.0.0.1:1"
    assert await jf.PROVIDER.detect(ctx) is False


def test_commands_tiers():
    cmds = {c.id: c for c in jf.PROVIDER.commands(_ctx())}
    assert cmds["jellyfin.search"].tier == 1
    assert cmds["jellyfin.play"].tier == 2
    assert cmds["jellyfin.control"].tier == 1


@respx.mock
async def test_search_returns_structured_results():
    respx.get(f"{BASE}/Items").mock(return_value=httpx.Response(200, json={"Items": [
        {"Id": "abc", "Name": "Dune", "ProductionYear": 2021, "CommunityRating": 8.0},
        {"Id": "def", "Name": "Arrival", "ProductionYear": 2016, "CommunityRating": 7.9},
    ]}))
    res = await jf.search(_ctx(), genre="Science Fiction", min_rating=7.5)
    assert res.success
    data = json.loads(res.output)
    assert data[0]["title"] == "Dune" and data[0]["id"] == "abc" and data[0]["rating"] == 8.0


async def test_search_without_api_key():
    ctx = _ctx()
    ctx.config.jellyfin.api_key = ""
    res = await jf.search(ctx)
    assert not res.success and "API key" in res.error


@respx.mock
async def test_control_pause_active_session():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "Dune"}},
    ]))
    route = respx.post(f"{BASE}/Sessions/s1/Playing/Pause").mock(return_value=httpx.Response(204))
    res = await jf.control(_ctx(), action="pause")
    assert res.success and route.called


@respx.mock
async def test_control_no_session():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    res = await jf.control(_ctx(), action="pause")
    assert not res.success and "No active" in res.error


@respx.mock
async def test_play_uses_existing_session_when_user_says_yes():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "tv1", "SupportsRemoteControl": True, "DeviceName": "Living Room TV"},
    ]))
    play_route = respx.post(f"{BASE}/Sessions/tv1/Playing").mock(return_value=httpx.Response(204))

    ctx = _ctx()
    vs = VoiceSession("s", None)
    ctx.voice_session = vs

    async def emit(ev):
        if ev.type == "confirm":
            vs.resolve(ev.id, True)   # "yes, use the open TV"
    ctx.voice_emit = emit

    res = await jf.play(ctx, item_id="abc")
    assert res.success and "Living Room TV" in res.output and play_route.called


@respx.mock
async def test_play_falls_back_to_browser_sign_in(monkeypatch):
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))  # nothing controllable
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)

    class _FakePage:
        async def goto(self, *a, **k): ...

    async def _fake_browser(ctx, profile="default"):
        return types.SimpleNamespace(pages=[_FakePage()])

    monkeypatch.setattr("gabagent.commands.browser.ensure_browser", _fake_browser)

    async def _no_sleep(*a, **k): ...
    monkeypatch.setattr(jf.asyncio, "sleep", _no_sleep)

    res = await jf.play(_ctx(), item_id="abc")
    assert "sign in" in res.output.lower()   # opened the window, asks for one-time login
