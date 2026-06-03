import asyncio
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
    def __init__(self, page): self.keys = []; self._page = page
    async def press(self, k):
        self.keys.append(k)
        if k == "Space":           # the real web player toggles play/pause on Space
            self._page.paused = not self._page.paused


class _FakePlayPage:
    """Models the HTML5 <video> we introspect via page.evaluate (paused + volume) and can close."""
    def __init__(self, paused=False, volume=1.0):
        self.paused = paused; self.volume = volume
        self.keyboard = _FakeKbd(self); self._closed = False
    def is_closed(self): return self._closed
    async def close(self): self._closed = True
    async def evaluate(self, expr, arg=None):
        if "v.pause()" in expr: self.paused = True; return None   # reliable pause (no gesture)
        if "v.volume = vol" in expr:          # setter
            self.volume = arg; return None
        if "v.paused" in expr: return self.paused
        if "v.volume" in expr: return self.volume
        return None


async def test_browser_control_pause_resume_stop():
    page = _FakePlayPage(paused=False)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_paused = False
    r = await jf.control(ctx, action="pause")
    assert r.success and page.keyboard.keys == ["Space"] and ctx.jellyfin_paused is True
    r = await jf.control(ctx, action="resume")
    assert r.success and page.keyboard.keys == ["Space", "Space"] and ctx.jellyfin_paused is False
    # stop now reliably pauses (video.pause()) + exits fullscreen (Escape), and KEEPS the window.
    r = await jf.control(ctx, action="stop")
    assert r.success and page.paused is True and page.keyboard.keys[-1] == "Escape"
    assert ctx.jellyfin_playing_page is page and ctx.jellyfin_paused is True


@respx.mock
async def test_browser_control_close_closes_owned_page():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="close")
    assert r.success and page._closed is True and ctx.jellyfin_playing_page is None
    assert r.output == "Closed the movie window."          # nothing else playing → plain close


@respx.mock
async def test_close_warns_when_another_unowned_session_still_playing():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "x", "NowPlayingItem": {"Name": "The Matrix"}, "PlayState": {"IsPaused": False}},
    ]))
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "You Only Live Twice"
    r = await jf.control(ctx, action="close")
    assert r.success and "another Jellyfin is still playing" in r.output


@respx.mock
async def test_close_quiet_when_only_just_closed_title_lingers():
    # Our own just-closed movie can linger in /Sessions for a beat — that must NOT be reported as another player.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "x", "NowPlayingItem": {"Name": "You Only Live Twice"}, "PlayState": {"IsPaused": False}},
    ]))
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "You Only Live Twice"
    r = await jf.control(ctx, action="close")
    assert r.success and r.output == "Closed the movie window."


async def test_browser_control_movie_volume_adjusts_video_and_duck_prior():
    from gabagent.voice.ducking import _state
    page = _FakePlayPage(volume=0.5)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="volume_up")
    assert r.success and abs(page.volume - 0.6) < 1e-9       # drives <video>.volume, not system
    await jf.control(ctx, action="volume_down")
    assert abs(page.volume - 0.5) < 1e-9
    # While ducked, a manual change updates the saved restore level so it survives speech-end.
    st = _state(ctx); st["jellyfin_video_volume"] = 1.0
    page.volume = 0.2
    await jf.control(ctx, action="volume_up")
    assert abs(page.volume - 0.3) < 1e-9 and abs(st["jellyfin_video_volume"] - 0.3) < 1e-9


async def test_browser_only_actions_need_owned_page():
    ctx = _ctx()  # no jellyfin_playing_page, no live REST session
    r = await jf.control(ctx, action="close")
    assert not r.success and "browser" in r.error.lower()


async def test_browser_control_idempotent_reads_real_state():
    # Already paused → "pause" is a no-op (reads real video.paused, doesn't blind-toggle).
    page = _FakePlayPage(paused=True)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="pause")
    assert "already paused" in r.output and page.keyboard.keys == []
    # Already playing → "resume" is a no-op too.
    page2 = _FakePlayPage(paused=False)
    ctx2 = _ctx(); ctx2.jellyfin_playing_page = page2
    r2 = await jf.control(ctx2, action="resume")
    assert "already playing" in r2.output and page2.keyboard.keys == []


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


@respx.mock
async def test_play_confirm_dedups_identical_device_names():
    """Two browser windows both named 'Chrome' must read as 'your open Chrome', not 'Chrome or Chrome'."""
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "c1", "SupportsRemoteControl": True, "DeviceName": "Chrome"},
        {"Id": "c2", "SupportsRemoteControl": True, "DeviceName": "Chrome"},
    ]))
    respx.post(f"{BASE}/Sessions/c1/Playing").mock(return_value=httpx.Response(204))
    ctx = _ctx()
    vs = VoiceSession("s", None)
    ctx.voice_session = vs
    seen = {}
    async def emit(ev):
        if ev.type == "confirm":
            seen["text"] = ev.summary
            vs.resolve(ev.id, True)
    ctx.voice_emit = emit
    await jf.play(ctx, item_id="abc")
    assert "Chrome or Chrome" not in seen["text"]
    assert seen["text"].count("Chrome") == 1


def test_now_playing_command_published():
    cmds = {c.id for c in jf.PROVIDER.commands(_ctx())}
    assert "jellyfin.now_playing" in cmds            # real command (model used to invent this id)


@respx.mock
async def test_now_playing_reports_title():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"NowPlayingItem": {"Name": "The Dark Knight"}, "PlayState": {"IsPaused": False}},
    ]))
    res = await jf.now_playing(_ctx(api_key="k"))
    assert res.success and "The Dark Knight" in res.output and "Playing" in res.output


@respx.mock
async def test_now_playing_nothing():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    res = await jf.now_playing(_ctx(api_key="k"))
    assert "Nothing is playing" in res.output


@respx.mock
async def test_play_existing_session_stores_title_for_window_targeting():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "tv1", "SupportsRemoteControl": True, "DeviceName": "Chrome"},
    ]))
    respx.post(f"{BASE}/Sessions/tv1/Playing").mock(return_value=httpx.Response(204))
    ctx = _ctx()
    vs = VoiceSession("s", None); ctx.voice_session = vs
    async def emit(ev):
        if ev.type == "confirm": vs.resolve(ev.id, True)   # yes, use the open Chrome (unowned)
    ctx.voice_emit = emit
    res = await jf.play(ctx, item_id="abc", title="12 Angry Men")
    assert res.success
    assert ctx.jellyfin_playing_title == "12 Angry Men"   # stored so window-ops can target the window


@respx.mock
async def test_control_stop_clears_playing_title():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "X"}, "SupportsRemoteControl": True},
    ]))
    respx.post(f"{BASE}/Sessions/s1/Playing/Stop").mock(return_value=httpx.Response(204))
    ctx = _ctx(); ctx.jellyfin_playing_title = "12 Angry Men"
    await jf.control(ctx, action="stop")
    assert ctx.jellyfin_playing_title is None


class _FSPage:
    """Owned page stub for exit-fullscreen: records evaluate() exprs and key presses."""
    def __init__(self): self.evals = []; self.keys = []; self._closed = False
    def is_closed(self): return self._closed
    async def evaluate(self, expr, *a): self.evals.append(expr)
    @property
    def keyboard(self):
        page = self
        class _K:
            async def press(self_, k): page.keys.append(k)
        return _K()


def test_control_publishes_exit_fullscreen():
    cmds = {c.id: c for c in jf.PROVIDER.commands(_ctx())}
    action = next(s for s in cmds["jellyfin.control"].params if s.name == "action")
    assert "exit_fullscreen" in action.enum


async def test_control_exit_fullscreen_owned_does_both_layers(monkeypatch):
    called = []
    async def fake_exit(ctx): called.append(1); return True
    monkeypatch.setattr("gabagent.commands.providers.desktop.exit_movie_fullscreen", fake_exit)
    ctx = _ctx(); page = _FSPage(); ctx.jellyfin_playing_page = page
    res = await jf.control(ctx, action="exit_fullscreen")
    assert res.success and "Left full screen" in res.output
    assert any("exitFullscreen" in e for e in page.evals)   # player HTML5 fullscreen
    assert "Escape" in page.keys                            # belt
    assert called                                           # KWin window fullscreen too


async def test_control_exit_fullscreen_unowned_is_honest(monkeypatch):
    async def fake_exit(ctx): return True                  # KWin drop succeeds
    monkeypatch.setattr("gabagent.commands.providers.desktop.exit_movie_fullscreen", fake_exit)
    ctx = _ctx(); ctx.jellyfin_playing_page = None; ctx.jellyfin_playing_title = "Pulp Fiction"
    res = await jf.control(ctx, action="exit_fullscreen")
    assert res.success and "press Escape" in res.output    # honest about the player layer we can't drive


async def test_page_eval_caps_a_hanging_evaluate(monkeypatch):
    """A wedged renderer (right after page.close(), or when an app-close moved the audio sink) must not
    hang the single shared voice loop — every page.evaluate is capped, falling back to a safe default."""
    monkeypatch.setattr(jf, "_EVAL_TIMEOUT", 0.05)
    class _HangPage:
        async def evaluate(self, expr, arg=None):
            await asyncio.sleep(5)                          # never returns within the cap
    page = _HangPage()
    assert await jf._page_eval(page, "() => 1", default="X") == "X"
    assert await jf._video_paused(page) is None            # read → safe None, no hang
    assert await jf._video_volume(page) is None
    assert await jf._set_video_volume(page, 0.5) is False   # set → False (distinguishable from success)


def test_live_jellyfin_page_none_after_close():
    """A closed page is detected and the stale ref cleared, so no eval is ever attempted on a dead page."""
    ctx = _ctx()
    class _ClosedPage:
        def is_closed(self): return True
    ctx.jellyfin_playing_page = _ClosedPage()
    assert jf._live_jellyfin_page(ctx) is None
    assert ctx.jellyfin_playing_page is None                # cleared
