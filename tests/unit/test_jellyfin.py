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
    assert cmds["jellyfin.play"].tier == 1   # playing media is reversible → no gate (surface confirm only when a client is open)
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


@respx.mock
async def test_title_search_skips_rating_floor_and_includes_tv():
    # Regression: a named title must not be filtered by the default rating floor, and TV is findable.
    route = respx.get(f"{BASE}/Items").mock(return_value=httpx.Response(200, json={"Items": []}))
    await jf.search(_ctx(), query="You Only Live Twice")
    params = route.calls.last.request.url.params
    assert "minCommunityRating" not in params
    assert "Series" in params["IncludeItemTypes"]


@respx.mock
async def test_genre_browse_still_applies_rating_floor():
    route = respx.get(f"{BASE}/Items").mock(return_value=httpx.Response(200, json={"Items": []}))
    await jf.search(_ctx(), genre="Action")
    assert route.calls.last.request.url.params["minCommunityRating"] == "7.0"


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


class _FakePage:
    """Stand-in for a Playwright page; `url`/password-field count drive the login check."""
    def __init__(self, url="http://jf.test/web/#/home.html", pw_count=0):
        self.url = url
        self._pw = pw_count

    async def goto(self, *a, **k): ...
    async def wait_for_timeout(self, *a, **k): ...

    def locator(self, sel):
        page = self
        class _Loc:
            async def count(self_inner): return page._pw
        return _Loc()


def _browser_with(page):
    async def _fake_browser(ctx, profile="default"):
        return types.SimpleNamespace(pages=[page])
    return _fake_browser


@respx.mock
async def test_play_not_signed_in_emits_blocked(monkeypatch):
    # No controllable client + the web player isn't signed in → a clear one-time setup `blocked`,
    # NOT a dead tab that times out.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)
    monkeypatch.setattr("gabagent.commands.browser.ensure_browser",
                        _browser_with(_FakePage(url="http://jf.test/web/#/login.html")))

    ctx = _ctx()
    emitted = []
    async def emit(ev): emitted.append(ev)
    ctx.voice_emit = emit
    ctx.voice_session = VoiceSession("s", None)

    res = await jf.play(ctx, item_id="abc")
    assert "sign in" in res.output.lower()
    assert any(e.type == "blocked" for e in emitted)   # surfaced as blocked, not a failed play


@respx.mock
async def test_play_signed_in_browser_path_does_not_crash(monkeypatch):
    # Regression: `events` was imported only inside the controllable-session branch, so the browser
    # fallback raised UnboundLocalError when a voice emit was set. Signed-in page → reaches the poll.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)
    monkeypatch.setattr("gabagent.commands.browser.ensure_browser", _browser_with(_FakePage()))

    async def _no_sleep(*a, **k): ...
    monkeypatch.setattr(jf.asyncio, "sleep", _no_sleep)

    ctx = _ctx()
    emitted = []
    async def emit(ev): emitted.append(ev)
    ctx.voice_emit = emit
    ctx.voice_session = VoiceSession("s", None)

    res = await jf.play(ctx, item_id="abc")            # must not raise UnboundLocalError
    assert res.output                                  # got a clean message, not a crash
    assert any(e.type == "status" for e in emitted)    # "Opening the player…" reached the emit


# -- browser-path control via Playwright (web client ignores remote-control API) ----

class _FakeKbd:
    def __init__(self): self.keys = []
    async def press(self, k): self.keys.append(k)


class _FakePlayPage:
    def __init__(self): self.keyboard = _FakeKbd(); self._closed = False
    def is_closed(self): return self._closed


async def test_browser_control_pause_resume_stop():
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_paused = False
    r = await jf.control(ctx, action="pause")
    assert r.success and page.keyboard.keys == ["Space"] and ctx.jellyfin_paused is True
    r = await jf.control(ctx, action="resume")
    assert r.success and page.keyboard.keys == ["Space", "Space"] and ctx.jellyfin_paused is False
    r = await jf.control(ctx, action="stop")
    assert r.success and page.keyboard.keys[-1] == "Escape" and ctx.jellyfin_playing_page is None


async def test_browser_control_idempotent_pause():
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_paused = True
    r = await jf.control(ctx, action="pause")
    assert "already paused" in r.output and page.keyboard.keys == []   # no double toggle


@respx.mock
async def test_control_uses_sessions_api_without_browser_page():
    # No browser page → falls back to the Sessions API (real controllable clients).
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "X"}}]))
    route = respx.post(f"{BASE}/Sessions/s1/Playing/Pause").mock(return_value=httpx.Response(204))
    ctx = _ctx()  # no jellyfin_playing_page attr
    res = await jf.control(ctx, action="pause")
    assert res.success and route.called


@respx.mock
async def test_authenticate_returns_token_and_server():
    respx.post(f"{BASE}/Users/AuthenticateByName").mock(return_value=httpx.Response(
        200, json={"AccessToken": "tok", "User": {"Id": "u1"}}))
    respx.get(f"{BASE}/System/Info/Public").mock(return_value=httpx.Response(200, json={"Id": "srv1"}))
    ctx = _ctx(); ctx.config.jellyfin.username = "rob"; ctx.config.jellyfin.password = "pw"
    auth = await jf._authenticate(ctx.config.jellyfin)
    assert auth == {"AccessToken": "tok", "UserId": "u1", "ServerId": "srv1"}


async def test_inject_auth_seeds_localstorage(monkeypatch):
    captured = {}
    class _BCtx:
        async def add_init_script(self, js): captured["js"] = js
    async def fake_auth(jc): return {"AccessToken": "tok", "UserId": "u1", "ServerId": "srv1"}
    monkeypatch.setattr(jf, "_authenticate", fake_auth)
    ok = await jf._inject_jellyfin_auth(_ctx().config.jellyfin, _BCtx())
    assert ok and "jellyfin_credentials" in captured["js"] and "tok" in captured["js"]
