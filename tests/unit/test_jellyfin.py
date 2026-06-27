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


@pytest.fixture(autouse=True)
def _no_real_movie_sink(monkeypatch):
    """Never let the movie-volume path touch real system audio: force the browser sink-input to be
    unidentifiable, so manual volume falls back to the deterministic <video>.volume page mock. Sink-path
    behavior is covered separately in test_ducking.py with a stubbed pactl router."""
    import gabagent.voice.ducking as _dk
    async def _none(_ctx):
        return None
    monkeypatch.setattr(_dk, "_jellyfin_sink_input", _none)
    # These tests exercise the owned-browser play path, which is the KDE/EM behavior; the new non-KDE guard
    # (jellyfin._is_kde_wayland_desktop) would otherwise short-circuit it in a headless/non-KDE test env.
    monkeypatch.setattr(jf, "_is_kde_wayland_desktop", lambda: True)


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


class _FakePage:
    """Stand-in for a Playwright page; `url`/password-field count drive the login check. Also models the
    HTML5 <video> the play path introspects after PlayNow (so _await_playback sees a real element):
    `has_video=False` models the library/home view, `paused=True` a player held by autoplay policy."""
    def __init__(self, url="http://jf.test/web/#/home.html", pw_count=0, has_video=True, paused=False):
        self.url = url
        self._pw = pw_count
        self.has_video = has_video; self.paused = paused
        self.keyboard = _FakeKbd(self); self._closed = False

    def is_closed(self): return self._closed
    async def close(self): self._closed = True
    async def goto(self, *a, **k): ...
    async def wait_for_timeout(self, *a, **k): ...

    async def evaluate(self, expr, arg=None):
        if "has_video" in expr:
            if not self.has_video:
                return {"has_video": False, "paused": None, "t": None, "ready": None,
                        "muted": None, "vol": None}
            return {"has_video": True, "paused": self.paused, "t": 1.0, "ready": 4,
                    "muted": False, "vol": 1.0}
        if "document.title = t" in expr: return None     # window self-naming stamp
        if "v.volume = vol" in expr: return True          # volume set succeeds (also clears muted)
        if "v.paused" in expr: return self.paused
        return None

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


def test_strip_vocative():
    assert jf._strip_vocative("play the movie Aria") == "play the movie"
    assert jf._strip_vocative("Aria play the matrix") == "play the matrix"
    assert jf._strip_vocative("hey Aria, the godfather") == "the godfather"
    assert jf._strip_vocative("the matrix") == "the matrix"           # untouched
    assert jf._strip_vocative("Aria") == "Aria"                       # bare vocative left for the caller
    assert jf._strip_vocative("") == ""
    # STT variants of the name (live transcripts): clipped "Ari", spelled "R.A.", "Arya"
    assert jf._strip_vocative("Ari play the matrix") == "play the matrix"
    assert jf._strip_vocative("hey R.A., the godfather") == "the godfather"
    assert jf._strip_vocative("R.A. play the matrix") == "play the matrix"
    assert jf._strip_vocative("Arya play the matrix") == "play the matrix"
    assert jf._strip_vocative("play the movie Ari") == "play the movie"
    # near-misses that merely START with the vocative letters must NOT be stripped
    assert jf._strip_vocative("Arizona Robbins") == "Arizona Robbins"
    assert jf._strip_vocative("Arrival") == "Arrival"


@respx.mock
async def test_search_strips_trailing_vocative_from_query():
    cap = {}
    def _resp(request):
        cap["term"] = request.url.params.get("SearchTerm")
        return httpx.Response(200, json={"Items": []})
    respx.get(f"{BASE}/Items").mock(side_effect=_resp)
    ctx = _ctx()
    await jf.search(ctx, query="the matrix Aria")
    assert cap["term"] == "the matrix"                                # vocative dropped before the search


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


@respx.mock
async def test_play_always_opens_owned_window_no_confirm_no_narration(monkeypatch):
    # play() never branches on the /Sessions list — that list usually reflects API sessions on OTHER devices
    # (a movie in another room), not a real local client we could hand off to. So it ALWAYS opens our owned
    # controllable window: no "play there?" confirm, no PlayNow to a detected client, and no Chrome/web
    # narration in the spoken result — just open the movie. (Rob, 2026-06-15.)
    web_play = respx.post(f"{BASE}/Sessions/web1/Playing").mock(return_value=httpx.Response(204))
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "web1", "SupportsRemoteControl": True, "DeviceName": "Chrome", "Client": "Jellyfin Web"},
    ]))
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)
    monkeypatch.setattr("gabagent.commands.browser.ensure_browser", _browser_with(_FakePage()))
    async def _no_sleep(*a, **k): ...
    monkeypatch.setattr(jf.asyncio, "sleep", _no_sleep)

    ctx = _ctx()
    emitted = []
    async def emit(ev): emitted.append(ev)
    ctx.voice_emit = emit
    ctx.voice_session = VoiceSession("s", None)

    res = await jf.play(ctx, item_id="abc", title="Hot Fuzz")
    assert res.output                                             # opened, no crash
    assert not any(e.type == "confirm" for e in emitted)         # never asks "play there?"
    assert not web_play.called                                   # never PlayNows a detected client
    # no Chrome/web bypass narration in the spoken result — just open the movie
    assert "chrome" not in (res.output or "").lower()
    assert "controllable window" not in (res.output or "").lower()
    status = next((e for e in emitted if e.type == "status"), None)
    assert status is not None and "player" in status.text.lower()   # plain "Opening the player…"


# -- browser-path control via Playwright (web client ignores remote-control API) ----

class _FakeKbd:
    def __init__(self, page): self.keys = []; self._page = page
    async def press(self, k):
        self.keys.append(k)
        if k == "Space":           # the real web player toggles play/pause on Space
            self._page.paused = not self._page.paused


class _FakePlayPage:
    """Models the HTML5 <video> we introspect via page.evaluate (paused + volume + seek) and can close.
    `has_video=False` models the library/home view where no <video> element exists yet."""
    def __init__(self, paused=False, volume=1.0, current_time=50.0, duration=100.0, has_video=True,
                 muted=False):
        self.paused = paused; self.volume = volume; self.muted = muted
        self.currentTime = current_time; self.duration = duration
        self.has_video = has_video
        self.keyboard = _FakeKbd(self); self._closed = False
    def is_closed(self): return self._closed
    async def close(self): self._closed = True
    async def evaluate(self, expr, arg=None):
        if "has_video" in expr:               # the _await_playback / audible state probe
            if not self.has_video:
                return {"has_video": False, "paused": None, "t": None, "ready": None,
                        "muted": None, "vol": None}
            return {"has_video": True, "paused": self.paused, "t": self.currentTime, "ready": 4,
                    "muted": self.muted, "vol": self.volume}
        if "document.title = t" in expr: return None              # window self-naming stamp
        if "v.pause()" in expr: self.paused = True; return None   # reliable pause (no gesture)
        if "v.currentTime =" in expr and arg is not None:
            if "v.currentTime + s" in expr:                       # relative seek (forward/back)
                self.currentTime = max(0, min(self.duration, self.currentTime + arg)); return None
            self.currentTime = max(0, min(self.duration, arg)); return True   # absolute seek_to
        if "v.volume = vol" in expr:          # setter also clears muted (make it audible); JS returns true
            self.volume = arg; self.muted = False; return True
        if "v.paused" in expr: return self.paused
        if "v.volume" in expr: return self.volume
        return None


async def test_await_playback_already_playing(monkeypatch):
    monkeypatch.setattr(jf, "_PLAYBACK_POLL_INTERVAL", 0)
    page = _FakePlayPage(paused=False)
    pb = await jf._await_playback(page)
    assert pb["started"] is True and pb["nudged"] is False
    assert page.keyboard.keys == []                     # no nudge needed


async def test_await_playback_nudges_autoplay_paused(monkeypatch):
    monkeypatch.setattr(jf, "_PLAYBACK_POLL_INTERVAL", 0)
    page = _FakePlayPage(paused=True)                   # player mounted but autoplay-blocked
    pb = await jf._await_playback(page)
    assert pb["started"] is True and pb["nudged"] is True
    assert page.keyboard.keys == ["Space"]              # one trusted gesture, then it played


async def test_await_playback_library_never_starts(monkeypatch):
    monkeypatch.setattr(jf, "_PLAYBACK_POLL_INTERVAL", 0)
    monkeypatch.setattr(jf, "_PLAYBACK_POLL_TRIES", 3)
    page = _FakePlayPage(has_video=False)               # still on the library — no <video> at all
    pb = await jf._await_playback(page)
    assert pb["started"] is False and pb["has_video"] is False and pb["nudged"] is False
    assert page.keyboard.keys == []                     # nothing to nudge


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


async def test_resume_unmutes_a_stranded_muted_element():
    # The muted-on-resume bug: a browser resume can come back with <video>.muted=true while the PipeWire
    # sink is at a normal level, so the movie plays silently. Resume must clear the mute so audio returns.
    page = _FakePlayPage(paused=True, muted=True, volume=0.85)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_paused = True
    r = await jf.control(ctx, action="resume")
    assert r.success and ctx.jellyfin_paused is False
    assert page.muted is False                    # unmuted → audible again
    assert abs(page.volume - 0.85) < 1e-9         # a real user level is preserved, not blown to full


async def test_resume_lifts_zero_volume_to_full():
    # If a resume strands the element at volume 0 (not just muted), bring it back to full so it's audible.
    page = _FakePlayPage(paused=True, muted=True, volume=0.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_paused = True
    r = await jf.control(ctx, action="resume")
    assert r.success and page.muted is False and abs(page.volume - 1.0) < 1e-9


async def test_set_video_volume_clears_muted():
    # A volume set means "make it audible at this level" — it must also clear the muted flag, else the
    # sink/video stay silent (the play-path _set_video_volume(1.0) relied on this to un-strand a mute).
    page = _FakePlayPage(muted=True, volume=0.3)
    ok = await jf._set_video_volume(page, 0.7)
    assert ok is True and page.muted is False and abs(page.volume - 0.7) < 1e-9


@respx.mock
async def test_browser_control_close_closes_owned_page():
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="close")
    assert r.success and page._closed is True and ctx.jellyfin_playing_page is None
    assert r.output == "Closed the movie window."          # nothing else playing → plain close


@respx.mock
async def test_close_does_not_volunteer_other_session_by_default():
    # SOP (jellyfin.announce_other_sessions default OFF): close reports what it did and does NOT volunteer
    # that a different unowned session is still playing. (Rob's saved memory rule, 2026-06-15.)
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "x", "NowPlayingItem": {"Name": "The Matrix"}, "PlayState": {"IsPaused": False}},
    ]))
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "You Only Live Twice"
    r = await jf.control(ctx, action="close")
    assert r.success and "closed" in r.output.lower()
    assert "another" not in r.output.lower() and "still playing" not in r.output.lower()


@respx.mock
async def test_close_warns_when_announce_other_sessions_enabled():
    # Opt-in restores the old "…but another is still playing" honesty notice.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "x", "NowPlayingItem": {"Name": "The Matrix"}, "PlayState": {"IsPaused": False}},
    ]))
    page = _FakePlayPage()
    ctx = _ctx(announce_other_sessions=True)
    ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "You Only Live Twice"
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
    # With the browser sink unidentifiable (guard forces no-sink), manual volume falls back to <video>.volume.
    from gabagent.voice.ducking import _state
    page = _FakePlayPage(volume=0.5)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="volume_up")
    assert r.success and abs(page.volume - 0.65) < 1e-9      # drives <video>.volume by the ±0.15 step
    await jf.control(ctx, action="volume_down")
    assert abs(page.volume - 0.5) < 1e-9
    # While ducked, a manual change steps the SAVED baseline (what the movie restores to) and leaves the
    # ducked <video> element alone — reading the ducked live value would corrupt the restore level.
    st = _state(ctx); st["jellyfin_video_volume"] = 0.6
    page.volume = 0.2                                       # the ducked live level
    await jf.control(ctx, action="volume_up")
    assert abs(st["jellyfin_video_volume"] - 0.75) < 1e-9   # baseline stepped 0.6→0.75
    assert abs(page.volume - 0.2) < 1e-9                    # ducked element untouched


async def test_browser_only_actions_need_owned_page():
    ctx = _ctx()  # no jellyfin_playing_page, no live REST session
    r = await jf.control(ctx, action="close")
    assert not r.success and "browser" in r.error.lower()


# -- routing: an owned-but-PAUSED page must not swallow a command meant for a DIFFERENT session ----
# (the live miss Rob hit 2026-06-14: "pause" kept hitting the already-paused local player while a movie
#  on another device kept playing).

@respx.mock
async def test_paused_owned_page_routes_pause_to_remote_native_session():
    # We own a paused movie ("Up"); a NATIVE client is actively playing "Shazam!" on another device.
    # `pause` must reach the remote session over REST, not press Space on the paused owned page.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "tv1", "Client": "Jellyfin Media Player", "DeviceName": "Bedroom TV",
         "NowPlayingItem": {"Name": "Shazam!"}, "PlayState": {"IsPaused": False}},
    ]))
    route = respx.post(f"{BASE}/Sessions/tv1/Playing/Pause").mock(return_value=httpx.Response(204))
    page = _FakePlayPage(paused=True)                       # owned movie is paused
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
    r = await jf.control(ctx, action="pause")
    assert r.success and route.called                       # REST hit the remote session…
    assert page.keyboard.keys == []                         # …and the owned page was NOT touched


@respx.mock
async def test_paused_owned_page_is_honest_about_remote_web_session():
    # The other movie is a remote WEB client — Jellyfin can't remote-control it (it 204s and ignores).
    # We must say so specifically, not falsely claim success and not poke the owned page.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "w1", "Client": "Jellyfin Web", "DeviceName": "Office Laptop",
         "NowPlayingItem": {"Name": "Shazam!"}, "PlayState": {"IsPaused": False},
         "SupportsRemoteControl": True},                    # web clients lie about this
    ]))
    posted = respx.post(f"{BASE}/Sessions/w1/Playing/Pause").mock(return_value=httpx.Response(204))
    page = _FakePlayPage(paused=True)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
    r = await jf.control(ctx, action="pause")
    assert not r.success
    assert "web browser" in r.error and "Office Laptop" in r.error
    assert not posted.called and page.keyboard.keys == []   # no false REST success, owned page untouched


async def test_playing_owned_page_still_controlled_directly():
    # Regression: when the owned movie IS playing, control it via the page with no session lookup at all
    # (no respx mock here — a stray /Sessions fetch would be a real network call / a bug).
    page = _FakePlayPage(paused=False)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
    r = await jf.control(ctx, action="pause")
    assert r.success and page.keyboard.keys == ["Space"] and ctx.jellyfin_paused is True


@respx.mock
async def test_paused_owned_page_with_nothing_else_playing_acts_on_owned():
    # Owned movie paused, no other session playing → "pause" stays on the owned page ("already paused").
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[]))
    page = _FakePlayPage(paused=True)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
    r = await jf.control(ctx, action="pause")
    assert r.success and "already paused" in r.output and page.keyboard.keys == []


@respx.mock
async def test_resume_targets_owned_paused_page_even_if_remote_playing():
    # `resume` means "un-pause the paused movie" = the owned page; it must NOT divert to a remote that's
    # already playing (no respx route for the remote → asserts we never REST it).
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "tv1", "Client": "Jellyfin Media Player", "DeviceName": "Bedroom TV",
         "NowPlayingItem": {"Name": "Shazam!"}, "PlayState": {"IsPaused": False}},
    ]))
    page = _FakePlayPage(paused=True)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
    r = await jf.control(ctx, action="resume")
    assert r.success and page.keyboard.keys == ["Space"] and ctx.jellyfin_paused is False


async def test_seek_on_paused_owned_page_never_diverts():
    # The live miss Rob hit 2026-06-17: pause the owned movie, then "skip ahead" → the seek used to divert
    # to a lingering web-client session and hard-fail ("…playing in a web browser…"). A skip is timeline-
    # specific to the movie you own and works fine while paused — it must seek the owned page, never divert.
    # No respx mock: a /Sessions fetch here would mean the divert path ran = a regression.
    for action, expected in (("forward", 80.0), ("back", 20.0), ("next", 80.0)):
        page = _FakePlayPage(paused=True, current_time=50.0, duration=100.0)
        ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
        r = await jf.control(ctx, action=action)
        assert r.success, action
        assert page.currentTime == expected, (action, page.currentTime)


async def test_seek_to_on_paused_owned_page_never_diverts():
    # Absolute jump on a paused owned movie also stays on the owned page (no divert / no REST lookup).
    page = _FakePlayPage(paused=True, current_time=50.0, duration=6000.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page; ctx.jellyfin_playing_title = "Up"
    r = await jf.control(ctx, action="seek_to", position=2700)   # jump to 45:00
    assert r.success and page.currentTime == 2700.0


@respx.mock
async def test_no_owned_page_honest_about_remote_web_session():
    # No owned page at all; the only thing playing is a remote WEB client → honest refusal, no false success.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "w1", "Client": "Jellyfin Web", "DeviceName": "Office Laptop",
         "NowPlayingItem": {"Name": "Shazam!"}, "PlayState": {"IsPaused": False}},
    ]))
    r = await jf.control(_ctx(), action="pause")
    assert not r.success and "web browser" in r.error and "Office Laptop" in r.error


async def test_browser_seek_forward_and_back_set_currenttime():
    page = _FakePlayPage(current_time=50.0, duration=100.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="forward")
    assert r.success and abs(page.currentTime - 80.0) < 1e-9 and "ahead" in r.output   # +30s, clamped to duration
    r = await jf.control(ctx, action="back")
    assert r.success and abs(page.currentTime - 50.0) < 1e-9 and "back" in r.output     # -30s


async def test_browser_next_aliases_to_skip_ahead():
    # A movie has no "next track" — "can you skip?" used to hard-error. Now bare next = skip ahead 30s.
    page = _FakePlayPage(current_time=50.0, duration=100.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="next")
    assert r.success and abs(page.currentTime - 80.0) < 1e-9 and "ahead" in r.output


async def test_fullscreen_owned_page_presses_f_and_kwin(monkeypatch):
    # Enter-fullscreen mirrors exit: press the player's own 'f' shortcut AND raise the KWin window.
    import gabagent.commands.providers.desktop as _desktop
    seen = {"kwin": False}
    async def _fake_fs(_ctx):
        seen["kwin"] = True
        return jf.ToolResult(output="kwin ok")
    monkeypatch.setattr(_desktop, "fullscreen", _fake_fs)
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="fullscreen")
    assert r.success and page.keyboard.keys == ["f"] and seen["kwin"] is True
    assert "full screen" in r.output.lower()


async def test_fullscreen_unowned_window_is_honest_about_player_layer(monkeypatch):
    # No owned page → KWin fullscreen only; be honest the player's own HTML5 layer may need a manual F.
    import gabagent.commands.providers.desktop as _desktop
    async def _fake_fs(_ctx):
        return jf.ToolResult(output="kwin ok")
    monkeypatch.setattr(_desktop, "fullscreen", _fake_fs)
    ctx = _ctx()  # no jellyfin_playing_page
    r = await jf.control(ctx, action="fullscreen")
    assert r.success and "press F" in r.output


async def test_fullscreen_reports_configured_movie_screen(monkeypatch):
    # When the movie window is moved to the configured screen, the spoken line names it ("on DP-1").
    import gabagent.commands.providers.desktop as _desktop
    async def _fake_fs(_ctx): return jf.ToolResult(output="kwin ok")
    async def _to_dp1(_ctx): return "DP-1"
    monkeypatch.setattr(_desktop, "fullscreen", _fake_fs)
    monkeypatch.setattr(_desktop, "to_movie_screen", _to_dp1)
    page = _FakePlayPage()
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="fullscreen")
    assert r.success and "on DP-1" in r.output


@respx.mock
async def test_play_auto_fullscreens_on_play(monkeypatch):
    # A movie that starts in a window we own is put full screen automatically (the default), and the
    # fullscreen outcome is folded into the result so the spoken confirmation is grounded.
    respx.get(f"{BASE}/Sessions").mock(side_effect=[
        httpx.Response(200, json=[]),                                       # _play_in_browser: `before`
        httpx.Response(200, json=[{"Id": "web1", "Client": "Jellyfin Web"}]),  # poll: new web session
    ])
    respx.post(f"{BASE}/Sessions/web1/Playing").mock(return_value=httpx.Response(204))
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)
    monkeypatch.setattr("gabagent.commands.browser.ensure_browser", _browser_with(_FakePage()))
    async def _no_login(_p): return False
    monkeypatch.setattr(jf, "_browser_needs_login", _no_login)
    async def _no_sleep(*a, **k): ...
    monkeypatch.setattr(jf.asyncio, "sleep", _no_sleep)
    called = {"fs": False}
    async def _fake_enter(_ctx):
        called["fs"] = True
        return jf.ToolResult(output="Set the movie to full screen on DP-1.")
    monkeypatch.setattr(jf, "_enter_fullscreen", _fake_enter)

    res = await jf.play(_ctx(), item_id="abc")
    assert res.success and called["fs"] is True
    assert "full screen on DP-1" in res.output     # grounded — narration matches the action taken


@respx.mock
async def test_play_is_honest_when_player_never_comes_up(monkeypatch):
    # PlayNow succeeded at the API level but the <video> never mounted (still on the library): don't
    # claim it's playing, and don't fullscreen the bare library.
    respx.get(f"{BASE}/Sessions").mock(side_effect=[
        httpx.Response(200, json=[]),                                          # _play_in_browser: `before`
        httpx.Response(200, json=[{"Id": "web1", "Client": "Jellyfin Web"}]),  # poll: new web session
    ])
    respx.post(f"{BASE}/Sessions/web1/Playing").mock(return_value=httpx.Response(204))
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)
    monkeypatch.setattr(jf, "_PLAYBACK_POLL_TRIES", 2)
    monkeypatch.setattr(jf, "_PLAYBACK_POLL_INTERVAL", 0)
    monkeypatch.setattr("gabagent.commands.browser.ensure_browser",
                        _browser_with(_FakePage(has_video=False)))
    async def _no_login(_p): return False
    monkeypatch.setattr(jf, "_browser_needs_login", _no_login)
    called = {"fs": False}
    async def _fake_enter(_ctx):
        called["fs"] = True
        return jf.ToolResult(output="Set the movie to full screen on DP-1.")
    monkeypatch.setattr(jf, "_enter_fullscreen", _fake_enter)

    res = await jf.play(_ctx(), item_id="abc")
    assert called["fs"] is False                    # never fullscreen the library
    assert "didn't come up" in res.output and "full screen" not in res.output


@respx.mock
async def test_play_respects_auto_fullscreen_off(monkeypatch):
    # With auto_fullscreen_movie disabled, a freshly-played movie stays windowed (no fullscreen call).
    respx.get(f"{BASE}/Sessions").mock(side_effect=[
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[{"Id": "web1", "Client": "Jellyfin Web"}]),
    ])
    respx.post(f"{BASE}/Sessions/web1/Playing").mock(return_value=httpx.Response(204))
    monkeypatch.setattr(jf, "_PLAY_POLL_TRIES", 1)
    monkeypatch.setattr("gabagent.commands.browser.ensure_browser", _browser_with(_FakePage()))
    async def _no_login(_p): return False
    monkeypatch.setattr(jf, "_browser_needs_login", _no_login)
    async def _no_sleep(*a, **k): ...
    monkeypatch.setattr(jf.asyncio, "sleep", _no_sleep)
    called = {"fs": False}
    async def _fake_enter(_ctx):
        called["fs"] = True
        return jf.ToolResult(output="x")
    monkeypatch.setattr(jf, "_enter_fullscreen", _fake_enter)

    ctx = _ctx(); ctx.config.desktop.auto_fullscreen_movie = False
    res = await jf.play(ctx, item_id="abc")
    assert res.success and called["fs"] is False


@respx.mock
async def test_remote_volume_uses_general_command():
    # No owned page → a native controllable session takes VolumeUp via the GeneralCommand endpoint.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "X"}}]))
    route = respx.post(f"{BASE}/Sessions/s1/Command").mock(return_value=httpx.Response(204))
    ctx = _ctx()  # no jellyfin_playing_page
    res = await jf.control(ctx, action="volume_up")
    assert res.success and route.called
    assert json.loads(route.calls.last.request.content) == {"Name": "VolumeUp"}


@respx.mock
async def test_remote_seek_uses_position_ticks():
    # No owned page → relative seek on a native client via PositionTicks (+30s = +3e8 ticks).
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "NowPlayingItem": {"Name": "X"}, "PlayState": {"PositionTicks": 100_000_000}}]))
    route = respx.post(f"{BASE}/Sessions/s1/Playing/Seek").mock(return_value=httpx.Response(204))
    ctx = _ctx()
    res = await jf.control(ctx, action="forward")
    assert res.success and route.called
    assert route.calls.last.request.url.params["seekPositionTicks"] == str(100_000_000 + 30 * 10_000_000)


def test_coerce_secs_and_fmt_hms():
    assert jf._coerce_secs(None) is None and jf._coerce_secs("") is None and jf._coerce_secs("oops") is None
    assert jf._coerce_secs(2700) == 2700 and jf._coerce_secs("4800") == 4800 and jf._coerce_secs(-5) == 0
    assert jf._coerce_secs(90.7) == 90
    # ms-slip guard: a value past the sane-seconds ceiling is folded down (the live FF regression).
    assert jf._coerce_secs(1_800_000) == 1800       # "30 minutes" handed to us as milliseconds
    assert jf._coerce_secs(60_000) == 60            # "1 minute" as ms
    assert jf._coerce_secs(14_400) == 14_400        # a legit 4h seconds value is NOT folded
    assert jf._fmt_hms(4800) == "1 hour 20 minutes"
    assert jf._fmt_hms(2700) == "45 minutes"
    assert jf._fmt_hms(150) == "2 minutes 30 seconds"
    assert jf._fmt_hms(3600) == "1 hour"
    assert jf._fmt_hms(45) == "45 seconds"


async def test_seek_to_owned_page_jumps_to_absolute_time():
    page = _FakePlayPage(paused=False, current_time=10.0, duration=7200.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="seek_to", position=2700)
    assert r.success and abs(page.currentTime - 2700) < 1e-9 and "45 minutes" in r.output


async def test_seek_to_owned_page_clamps_to_duration():
    page = _FakePlayPage(paused=False, current_time=10.0, duration=7200.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    # 10000s is a real (if past-the-end) seconds value — below the ms-fold ceiling — so it exercises the
    # duration clamp, not the unit guard.
    r = await jf.control(ctx, action="seek_to", position=10000)
    assert r.success and abs(page.currentTime - 7200) < 1e-9


async def test_seek_to_folds_milliseconds_to_seconds():
    # The live regression: model passes "30 minutes" as 1_800_000 (ms). Must land at 30:00, not 500 hours.
    page = _FakePlayPage(paused=False, current_time=10.0, duration=7200.0)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="seek_to", position=1_800_000)
    assert r.success and abs(page.currentTime - 1800) < 1e-9 and "30 minutes" in r.output
    assert "hour" not in r.output                      # never narrates the absurd "500 hours"


async def test_seek_to_without_position_asks_for_a_time():
    page = _FakePlayPage(paused=False)
    ctx = _ctx(); ctx.jellyfin_playing_page = page
    r = await jf.control(ctx, action="seek_to")
    assert not r.success and "time" in (r.error or "").lower()


@respx.mock
async def test_seek_to_remote_uses_absolute_position_ticks():
    # No owned page → absolute seek on a native client: seekPositionTicks = target seconds × 1e7.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "s1", "Client": "Jellyfin Android", "NowPlayingItem": {"Name": "X"},
         "PlayState": {"IsPaused": False}}]))
    route = respx.post(f"{BASE}/Sessions/s1/Playing/Seek").mock(return_value=httpx.Response(204))
    ctx = _ctx()
    res = await jf.control(ctx, action="seek_to", position=2700)
    assert res.success and route.called
    assert route.calls.last.request.url.params["seekPositionTicks"] == str(2700 * 10_000_000)


@respx.mock
async def test_play_to_session_passes_start_position_ticks():
    route = respx.post(f"{BASE}/Sessions/s1/Playing").mock(return_value=httpx.Response(204))
    jc = _ctx().config.jellyfin
    res = await jf._play_to_session(jc, "s1", "item1", "your TV", 4800)
    assert res.success and route.called
    assert route.calls.last.request.url.params["startPositionTicks"] == str(4800 * 10_000_000)
    assert "1 hour 20 minutes" in res.output


@respx.mock
async def test_play_to_session_no_position_omits_ticks():
    route = respx.post(f"{BASE}/Sessions/s1/Playing").mock(return_value=httpx.Response(204))
    jc = _ctx().config.jellyfin
    res = await jf._play_to_session(jc, "s1", "item1", "your TV")
    assert res.success and "startPositionTicks" not in route.calls.last.request.url.params


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
async def test_now_playing_summarizes_all_devices():
    # Every session across devices is reported — with device, state, and position — not just the first.
    respx.get(f"{BASE}/Sessions").mock(return_value=httpx.Response(200, json=[
        {"Id": "a", "DeviceName": "Living Room TV",
         "NowPlayingItem": {"Name": "Dune", "RunTimeTicks": 3600 * 10_000_000},
         "PlayState": {"IsPaused": False, "PositionTicks": 600 * 10_000_000}},
        {"Id": "b", "DeviceName": "Bedroom",
         "NowPlayingItem": {"Name": "Edge of Tomorrow"},
         "PlayState": {"IsPaused": True}},
        {"Id": "c", "DeviceName": "Idle Phone"},   # no NowPlayingItem → skipped
    ]))
    res = await jf.now_playing(_ctx(api_key="k"))
    assert "Living Room TV: Playing — Dune (10:00 of 1:00:00)" in res.output
    assert "Bedroom: Paused — Edge of Tomorrow" in res.output
    assert "Idle Phone" not in res.output           # nothing playing there


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
