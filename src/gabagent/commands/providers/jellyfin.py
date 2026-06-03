"""First-party Jellyfin provider: search/control via REST, play-on-screen via the persistent
browser. Uses code backends (PyBackend/BrowserBackend) — so it ships trusted (not attested),
unlike third-party declarative skills.
"""
from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, Slot, Detect, PyBackend, BrowserBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_CONTROL = {"pause": "Pause", "resume": "Unpause", "stop": "Stop", "next": "NextTrack"}
_BROWSER_ONLY = {"close", "volume_up", "volume_down"}   # only meaningful on the page we own
_SPECIAL = {"exit_fullscreen"}                           # handled directly (two fullscreen layers)
_ACTIONS = set(_CONTROL) | _BROWSER_ONLY | _SPECIAL
_PLAY_POLL_TRIES = 24   # ~12s waiting for the opened web client to register a session


class JellyfinProvider:
    id = "jellyfin"

    async def detect(self, ctx: AgentContext) -> bool:
        jc = getattr(ctx.config, "jellyfin", None)
        if not jc or not jc.enabled:
            return False
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(jc.base_url.rstrip("/") + "/System/Info/Public")
                return r.status_code == 200
        except Exception:
            return False

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(
                id="jellyfin.search", domain="media", tier=1, structured=True, featured=True,
                summary="Search the Jellyfin movie library by genre and minimum rating",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:search"),
                detect=Detect(),
                params=[
                    Slot("genre", "string", False, description="e.g. 'Science Fiction'"),
                    Slot("min_rating", "number", False, description="minimum IMDb/community rating 0–10"),
                    Slot("query", "string", False, description="title search term"),
                    Slot("unwatched", "boolean", False),
                ],
                examples=["find a 4-star sci-fi movie", "what comedies do I have rated over 8"],
            ),
            Command(
                id="jellyfin.play", domain="media", tier=1, requires_confirm_surface=True, featured=True,
                summary="Play a movie — on your open client if you have one, or in a new window",
                confirm_template="Play {title} in Jellyfin?",
                backend=BrowserBackend(ref="gabagent.commands.providers.jellyfin:play"),
                params=[
                    Slot("item_id", "string", True, description="Jellyfin item id from jellyfin.search"),
                    Slot("title", "string", False,
                         description="human movie title from jellyfin.search, for the spoken confirmation"),
                ],
                examples=["play that one", "play the first movie"],
            ),
            Command(
                id="jellyfin.control", domain="media", tier=1, featured=True,
                summary="Control the movie — pause, resume, stop, leave full screen, close the window, or turn the movie volume up/down",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:control"),
                params=[Slot("action", "enum", True,
                             enum=("pause", "resume", "stop", "next", "exit_fullscreen", "close",
                                   "volume_up", "volume_down"),
                             description="required — one of pause/resume/stop/next/exit_fullscreen/"
                             "close/volume_up/volume_down (exit_fullscreen = leave the movie's full screen)")],
                examples=["pause", "resume", "stop the movie", "leave full screen", "exit fullscreen",
                          "get out of full screen", "close the movie window",
                          "turn up the movie", "louder on the movie", "movie volume down"],
            ),
            Command(
                id="jellyfin.now_playing", domain="media", tier=1,
                summary="What movie is currently playing in Jellyfin",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:now_playing"),
                examples=["what are we watching", "what movie is this", "what's playing on Jellyfin"],
            ),
        ]


PROVIDER = JellyfinProvider()


# -- backend callables -----------------------------------------------------

def _client(jc) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=jc.base_url.rstrip("/"),
        headers={"Authorization": f'MediaBrowser Token="{jc.api_key}"'},
        timeout=15.0,
    )


async def now_playing(ctx) -> ToolResult:
    """The currently-playing Jellyfin movie title (the model kept inventing this id; now it's real)."""
    jc = ctx.config.jellyfin
    if not jc.enabled or not jc.api_key:
        return ToolResult(output="", error="Jellyfin isn't set up.")
    try:
        for s in await _sessions(jc):
            item = s.get("NowPlayingItem")
            if item and item.get("Name"):
                paused = (s.get("PlayState") or {}).get("IsPaused")
                return ToolResult(output=f"{'Paused' if paused else 'Playing'}: {item['Name']}")
    except Exception:
        return ToolResult(output="", error="I couldn't reach Jellyfin.")
    return ToolResult(output="Nothing is playing in Jellyfin right now.")


async def _sessions(jc) -> list[dict]:
    try:
        async with _client(jc) as c:
            r = await c.get("/Sessions")
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


async def search(ctx, genre=None, min_rating=None, query=None, unwatched=False) -> ToolResult:
    jc = ctx.config.jellyfin
    if not jc.api_key:
        return ToolResult(output="", error="Jellyfin API key not set (settings: jellyfin.api_key).")
    params = {
        "IncludeItemTypes": "Movie,Series", "Recursive": "true",
        "Fields": "Genres,CommunityRating,ProductionYear", "SortBy": "Random", "Limit": "20",
    }
    # The rating floor is for browse/recommendation ("find a good sci-fi"), NOT for an explicit
    # title lookup — otherwise a title the user named silently vanishes if it's rated below the
    # threshold (this is why "You Only Live Twice" (~6.8) couldn't be found under the 7.0 default).
    mr = min_rating if min_rating is not None else (None if query else jc.rating_threshold)
    if mr:
        params["minCommunityRating"] = str(mr)
    if genre:
        params["Genres"] = genre
    if query:
        params["SearchTerm"] = query
    if unwatched and jc.user_id:
        params["IsPlayed"] = "false"
        params["userId"] = jc.user_id
    try:
        async with _client(jc) as c:
            r = await c.get("/Items", params=params)
    except Exception as e:
        return ToolResult(output="", error=f"Jellyfin unreachable: {e}")
    if r.status_code != 200:
        return ToolResult(output="", error=f"Jellyfin search failed: HTTP {r.status_code}")
    items = r.json().get("Items", [])
    results = [
        {"id": i.get("Id"), "title": i.get("Name"), "year": i.get("ProductionYear"), "rating": i.get("CommunityRating")}
        for i in items
    ]
    return ToolResult(output=json.dumps(results))


async def _exit_fullscreen(ctx) -> ToolResult:
    """Leave full screen — two stacked layers: the Jellyfin web player's own (HTML5) fullscreen and the
    KWin window fullscreen. Owned page: exit both reliably. Unowned window: drop the KWin fullscreen and
    be honest that the player's own fullscreen may need a manual Escape."""
    from gabagent.commands.providers.desktop import exit_movie_fullscreen
    page = _live_jellyfin_page(ctx)
    if page is not None:
        try:
            await page.evaluate("() => { if (document.exitFullscreen) document.exitFullscreen(); }")
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await exit_movie_fullscreen(ctx)   # also drop any KWin window fullscreen
        return ToolResult(output="Left full screen.")
    if await exit_movie_fullscreen(ctx):
        return ToolResult(output="I've taken the movie window out of full screen. If the player itself "
                          "is still full screen, press Escape — I can't drive a window I didn't open.")
    return ToolResult(output="", error="I couldn't find the movie window to leave full screen.")


async def control(ctx, action="") -> ToolResult:
    jc = ctx.config.jellyfin
    if action not in _ACTIONS:
        return ToolResult(output="", error=f"unknown action: {action}")
    if action == "exit_fullscreen":
        return await _exit_fullscreen(ctx)
    # The Jellyfin web client ignores remote-control API commands (returns 204, does nothing — a
    # known upstream issue), so when WE launched the movie in our own browser, control it through
    # the page instead. Native/controllable clients still go through the Sessions API below.
    page = _live_jellyfin_page(ctx)
    if page is not None:
        return await _browser_control(ctx, page, action)
    if action in _BROWSER_ONLY:
        return ToolResult(output="", error="I can only do that to a movie I opened in the browser.")
    sessions = await _sessions(jc)
    target = next((s for s in sessions if s.get("NowPlayingItem")), None) \
        or next((s for s in sessions if s.get("SupportsRemoteControl")), None)
    if not target:
        return ToolResult(output="", error="No active playback session to control.")
    if action in ("stop", "close"):
        ctx.jellyfin_playing_title = None
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{target['Id']}/Playing/{_CONTROL[action]}")
    except Exception as e:
        return ToolResult(output="", error=f"control failed: {e}")
    return ToolResult(output=f"{action} sent.") if r.is_success else ToolResult(output="", error=f"control failed: HTTP {r.status_code}")


async def play(ctx, item_id="", title="") -> ToolResult:
    # `title` is carried for the spoken confirmation only; playback uses item_id.
    from gabagent.voice import events  # used by both the surface-confirm and the browser path
    jc = ctx.config.jellyfin
    if not item_id:
        return ToolResult(output="", error="no item to play")
    emit = getattr(ctx, "voice_emit", None)
    vs = getattr(ctx, "voice_session", None)

    sessions = await _sessions(jc)
    controllable = [s for s in sessions if s.get("SupportsRemoteControl") and s.get("DeviceName")]

    # Ask-if-ambiguous: a client is already open — use it, or open a new window?
    if controllable and vs is not None and emit is not None:
        # Dedup identical device names so two browser windows don't read as "Chrome or Chrome".
        names = " or ".join(dict.fromkeys(s.get("DeviceName", "a device") for s in controllable))
        what = f"{title} " if title else ""
        cid = uuid4().hex
        fut = vs.new_confirm(cid)
        # This confirm carries its own choice (yes=use it / no=new window), so it's a
        # complete spoken line — the client must not append the standard yes/no tail.
        await emit(events.confirm(
            cid, 2, "spoken_yesno",
            f"Play {what}on your open {names}? Say yes to play there, or no to open a new window.",
            "", prompt_complete=True))
        try:
            approved, _ = await asyncio.wait_for(fut, timeout=60)
        except Exception:
            approved = False
        if approved:
            res = await _play_to_session(jc, controllable[0]["Id"], item_id,
                                         controllable[0].get("DeviceName", "your client"))
            if res.success and title:
                # Remember the title even though we DON'T own this page (existing Chrome): it's the only
                # handle window-ops have to target the movie window by caption (vs. the active window).
                ctx.jellyfin_playing_title = title
            return res

    if emit:
        await emit(events.status("Opening the player…"))  # narrate before the slow launch
    res = await _play_in_browser(ctx, jc, item_id)
    if res.success and title:
        ctx.jellyfin_playing_title = title
    return res


async def _play_to_session(jc, session_id, item_id, label) -> ToolResult:
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session_id}/Playing", params={"itemIds": item_id, "playCommand": "PlayNow"})
    except Exception as e:
        return ToolResult(output="", error=f"play failed: {e}")
    return ToolResult(output=f"Playing on {label}.") if r.is_success else ToolResult(output="", error=f"play failed: HTTP {r.status_code}")


async def _play_in_browser(ctx, jc, item_id) -> ToolResult:
    from gabagent.commands.browser import ensure_browser
    from gabagent.voice import events
    emit = getattr(ctx, "voice_emit", None)

    before = {s["Id"] for s in await _sessions(jc)}
    try:
        bctx = await ensure_browser(ctx, profile="jellyfin")
        # Optional hands-free auth: pre-seed the web client's credentials so a fresh profile is
        # already signed in (best-effort; falls back to the one-time manual sign-in below).
        if jc.username and jc.password:
            await _inject_jellyfin_auth(jc, bctx)
        page = bctx.pages[0] if bctx.pages else await bctx.new_page()
        await page.goto(jc.base_url.rstrip("/") + "/web/", wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't open the player: {e}")

    # Don't open a dead player and time out: if the web client isn't signed in, surface a
    # one-time setup instruction as a `blocked` (the persistent profile remembers the login).
    if await _browser_needs_login(page):
        msg = ("I've opened Jellyfin in a browser, but it isn't signed in yet. Please sign in there "
               "once — I'll remember it — then ask me to play again.")
        if emit:
            await emit(events.blocked("jellyfin.play", msg))
        return ToolResult(output=msg)

    # Signed in: wait for the web client to register a session, then command it.
    session_id = None
    for _ in range(_PLAY_POLL_TRIES):
        await asyncio.sleep(0.5)
        for s in await _sessions(jc):
            if s["Id"] not in before and "web" in (s.get("Client", "").lower()):
                session_id = s["Id"]
                break
        if session_id:
            break
    if not session_id:
        return ToolResult(output="I opened the Jellyfin window but couldn't take control of it — give it a moment and ask again.")
    result = await _play_to_session(jc, session_id, item_id, "the new window")
    if result.success:
        # Remember the page so pause/resume/stop can drive it directly (the web client ignores
        # the remote-control API).
        ctx.jellyfin_playing_page = page
        ctx.jellyfin_paused = False
        # Start at full volume — the persistent browser reuses the same <video> element, so a duck
        # that got stranded low in a prior session shouldn't leave a freshly-played movie quiet.
        await _set_video_volume(page, 1.0)
    return result


_AUTH_HEADER = ('MediaBrowser Client="gabagent", Device="gabagent voice", '
                'DeviceId="gabagent-voice", Version="0.1.0"')


async def _authenticate(jc) -> dict | None:
    """Log in via the API and return {AccessToken, UserId, ServerId}, or None on failure."""
    try:
        async with httpx.AsyncClient(base_url=jc.base_url.rstrip("/"), timeout=10.0) as c:
            r = await c.post("/Users/AuthenticateByName",
                             json={"Username": jc.username, "Pw": jc.password},
                             headers={"X-Emby-Authorization": _AUTH_HEADER})
            if r.status_code != 200:
                return None
            d = r.json()
            info = await c.get("/System/Info/Public")
            sid = info.json().get("Id", "") if info.status_code == 200 else ""
    except Exception:
        return None
    return {"AccessToken": d.get("AccessToken"), "UserId": (d.get("User") or {}).get("Id"), "ServerId": sid}


async def _inject_jellyfin_auth(jc, bctx) -> bool:
    """Best-effort: seed the Jellyfin 10.x web client's localStorage so it boots signed-in.
    Format targets jellyfin-web 10.x; needs a live confirm, and is harmless if wrong (the page
    just shows login and the one-time manual path takes over)."""
    auth = await _authenticate(jc)
    if not auth or not auth.get("AccessToken"):
        return False
    creds = {"Servers": [{
        "Id": auth["ServerId"], "AccessToken": auth["AccessToken"], "UserId": auth["UserId"],
        "ManualAddress": jc.base_url.rstrip("/"), "LastConnectionMode": 1,
    }]}
    js = f"try{{localStorage.setItem('jellyfin_credentials', {json.dumps(json.dumps(creds))});}}catch(e){{}}"
    try:
        await bctx.add_init_script(js)
        return True
    except Exception:
        return False


def _live_jellyfin_page(ctx):
    """The Playwright page of a browser-launched movie, if one is still open; else None."""
    page = getattr(ctx, "jellyfin_playing_page", None)
    if page is None:
        return None
    try:
        if page.is_closed():
            ctx.jellyfin_playing_page = None
            return None
    except Exception:
        ctx.jellyfin_playing_page = None
        return None
    return page


# -- HTML5 <video> introspection on the page we own (idempotent control + volume-duck) ----------
# Reading the real element state is what stops a manual "pause" and the speech-ducking from fighting:
# both reconcile to the actual video, instead of guessing via a bool and a blind Space toggle.

async def _video_paused(page) -> bool | None:
    try:
        return await page.evaluate(
            "() => { const v = document.querySelector('video'); return v ? v.paused : null; }")
    except Exception:
        return None


async def _video_volume(page) -> float | None:
    try:
        return await page.evaluate(
            "() => { const v = document.querySelector('video'); return v ? v.volume : null; }")
    except Exception:
        return None


async def _set_video_volume(page, vol: float) -> bool:
    try:
        await page.evaluate(
            "(vol) => { const v = document.querySelector('video'); if (v) v.volume = vol; }", vol)
        return True
    except Exception:
        return False


async def _browser_control(ctx, page, action) -> ToolResult:
    try:
        if action == "stop":
            # Reliably pause (video.pause() needs no user gesture, unlike resume) and drop the player
            # out of fullscreen — but keep the window open. Escape alone (the old behavior) didn't
            # actually halt playback.
            await page.evaluate("() => { const v = document.querySelector('video'); if (v) v.pause(); }")
            await page.keyboard.press("Escape")
            ctx.jellyfin_paused = True
            return ToolResult(output="Paused the movie and came out of fullscreen.")
        if action == "close":
            try:
                await page.close()
            except Exception:
                pass
            ctx.jellyfin_playing_page = None
            ctx.jellyfin_paused = False
            ctx.jellyfin_playing_title = None
            return ToolResult(output="Closed the movie window.")
        if action in ("volume_up", "volume_down"):
            cur = await _video_volume(page)
            if cur is None:
                return ToolResult(output="", error="I couldn't read the movie volume.")
            new = min(1.0, cur + 0.1) if action == "volume_up" else max(0.0, cur - 0.1)
            await _set_video_volume(page, new)
            # If we're mid-duck, update the saved restore level so the manual change survives the
            # speech-end restore instead of being clobbered back to the pre-duck level.
            from gabagent.voice.ducking import _state
            st = _state(ctx)
            if st.get("jellyfin_video_volume") is not None:
                st["jellyfin_video_volume"] = new
            return ToolResult(output="Turned the movie up." if action == "volume_up" else "Turned the movie down.")
        if action == "next":
            return ToolResult(output="", error="There's nothing to skip to in a movie.")
        # Idempotent: read the REAL play state, then press Space (a toggle — but a *trusted* user
        # gesture, unlike video.play() via evaluate which can be autoplay-blocked) only if the state
        # actually needs to change. cur is None when we can't read it → best-effort single press.
        cur = await _video_paused(page)
        if action == "pause":
            if cur is True:
                ctx.jellyfin_paused = True
                return ToolResult(output="It's already paused.")
            await page.keyboard.press("Space")
            ctx.jellyfin_paused = True
            return ToolResult(output="Paused the movie.")
        if action == "resume":
            if cur is False:
                ctx.jellyfin_paused = False
                return ToolResult(output="It's already playing.")
            await page.keyboard.press("Space")
            ctx.jellyfin_paused = False
            return ToolResult(output="Resumed the movie.")
        return ToolResult(output="", error=f"unknown action: {action}")
    except Exception as e:
        return ToolResult(output="", error=f"couldn't control the player: {e}")


async def _browser_needs_login(page) -> bool:
    """True if the Jellyfin web client is showing its login/setup screen (not authenticated).
    The SPA routes to a #/login.html hash when there's no saved session."""
    try:
        await page.wait_for_timeout(1500)  # let the SPA settle / redirect
        url = (getattr(page, "url", "") or "").lower()
        if "login" in url or "wizard" in url:
            return True
        return await page.locator("input[type=password]").count() > 0
    except Exception:
        return False
