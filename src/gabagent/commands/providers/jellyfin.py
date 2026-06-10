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
_REMOTE_VOLUME = {"volume_up": "VolumeUp", "volume_down": "VolumeDown"}  # GeneralCommand for native clients
_BROWSER_ONLY = {"close"}                                # only meaningful on the page we own
_SEEK = {"forward": 30, "back": -30}                     # ±seconds; owned page via currentTime, REST via ticks
_SPECIAL = {"fullscreen", "exit_fullscreen"}             # handled directly (two fullscreen layers)
_ACTIONS = set(_CONTROL) | set(_REMOTE_VOLUME) | _BROWSER_ONLY | set(_SEEK) | _SPECIAL
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

    async def sources(self, ctx: AgentContext):
        """Media sources Jellyfin knows about. A movie WE launched (owned browser page) is local+owned.
        Otherwise every /Sessions entry with a NowPlayingItem is reported with a LOCALITY verdict
        (loopback endpoint / our DeviceId / configured device-name → local; else remote) and owned=False —
        so a session on another device is visible for future explicit control but never auto-touched.
        Never raises."""
        jc = getattr(ctx.config, "jellyfin", None)
        if not jc or not getattr(jc, "enabled", True) or not jc.api_key:
            return []
        from gabagent.commands.media import MediaSource, judge_locality
        page = _live_jellyfin_page(ctx)
        if page is not None:
            paused = await _video_paused(page)
            return [MediaSource(provider="jellyfin", kind="video",
                                state="paused" if paused is True else "playing",
                                owned=True, locality="local",
                                title=getattr(ctx, "jellyfin_playing_title", "") or "")]
        out = []
        try:
            for s in await _sessions(jc):
                npi = s.get("NowPlayingItem")
                if not npi:
                    continue
                paused = (s.get("PlayState") or {}).get("IsPaused")
                out.append(MediaSource(
                    provider="jellyfin", kind="video",
                    state="paused" if paused else "playing", owned=False,
                    locality=judge_locality(ctx, device_id=s.get("DeviceId", ""),
                                            device_name=s.get("DeviceName", ""),
                                            endpoint=s.get("RemoteEndPoint", "")),
                    device_name=s.get("DeviceName", ""), device_id=s.get("DeviceId", ""),
                    endpoint=s.get("RemoteEndPoint", ""), session_key=s.get("Id", ""),
                    title=(npi.get("Name") or "")))
        except Exception:
            pass
        return out

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
                summary="Control the movie — pause, resume, stop, fast-forward or rewind, enter or leave full screen, close the window, or turn the movie volume up/down",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:control"),
                params=[Slot("action", "enum", True,
                             enum=("pause", "resume", "stop", "next", "forward", "back",
                                   "fullscreen", "exit_fullscreen", "close", "volume_up", "volume_down"),
                             description="required — one of pause/resume/stop/next/forward/back/"
                             "fullscreen/exit_fullscreen/close/volume_up/volume_down (forward/back/next = "
                             "skip ±30s, fullscreen = put the movie full screen, exit_fullscreen = leave it)")],
                examples=["pause", "resume", "stop the movie", "fast forward", "skip ahead",
                          "rewind", "go back thirty seconds", "full screen the movie", "go fullscreen",
                          "leave full screen", "exit fullscreen", "get out of full screen",
                          "close the movie window", "turn up the movie", "louder on the movie", "movie volume down"],
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


async def _other_session_playing(jc, exclude_title: str = "") -> bool:
    """True if some Jellyfin session is actively playing — used after closing our own window to be honest
    that an unowned client is still going. Skips any session still showing the title we just closed (our
    own session can linger in /Sessions for a moment after the page closes), so it won't false-positive on
    the movie we just shut. Best-effort; never raises."""
    want = exclude_title.strip().lower()
    try:
        for s in await _sessions(jc):
            item = s.get("NowPlayingItem")
            if not item or (s.get("PlayState") or {}).get("IsPaused"):
                continue
            if want and (item.get("Name") or "").strip().lower() == want:
                continue          # our just-closed movie still settling — not a separate player
            return True
    except Exception:
        return False
    return False


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


async def _enter_fullscreen(ctx) -> ToolResult:
    """Enter full screen — the mirror of _exit_fullscreen, across the same two stacked layers. Owned page:
    press the player's own fullscreen shortcut ('f' — a TRUSTED key gesture, like Space for pause;
    video.requestFullscreen() via evaluate is blocked without user activation) and raise the window to KWin
    fullscreen. Unowned window: KWin fullscreen only, and be honest the player's own layer may need a manual 'f'."""
    from gabagent.commands.providers.desktop import fullscreen as _kwin_fullscreen, to_movie_screen
    moved = await to_movie_screen(ctx)   # put it on the configured movie screen (DP-1) first, if it's connected
    where = f" on {moved}" if moved else ""
    page = _live_jellyfin_page(ctx)
    if page is not None:
        try:
            await page.keyboard.press("f")
        except Exception:
            pass
        await _kwin_fullscreen(ctx)   # also raise the window to KWin fullscreen
        return ToolResult(output=f"Set the movie to full screen{where}.")
    res = await _kwin_fullscreen(ctx)
    if not res.error:
        return ToolResult(output=f"I've put the movie window in full screen{where}. If the player itself isn't "
                          "full screen, press F — I can't drive a window I didn't open.")
    return res


async def _exit_fullscreen(ctx) -> ToolResult:
    """Leave full screen — two stacked layers: the Jellyfin web player's own (HTML5) fullscreen and the
    KWin window fullscreen. Owned page: exit both reliably. Unowned window: drop the KWin fullscreen and
    be honest that the player's own fullscreen may need a manual Escape."""
    from gabagent.commands.providers.desktop import exit_movie_fullscreen
    page = _live_jellyfin_page(ctx)
    if page is not None:
        try:
            await _page_eval(page, "() => { if (document.exitFullscreen) document.exitFullscreen(); }")
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
    if action == "fullscreen":
        return await _enter_fullscreen(ctx)
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
    # No owned page → drive a native/controllable Jellyfin client over REST.
    sessions = await _sessions(jc)
    target = next((s for s in sessions if s.get("NowPlayingItem")), None) \
        or next((s for s in sessions if s.get("SupportsRemoteControl")), None)
    if not target:
        return ToolResult(output="", error="No active playback session to control.")
    if action in _REMOTE_VOLUME:
        return await _rest_general_command(jc, target["Id"], _REMOTE_VOLUME[action])
    if action in _SEEK:
        return await _rest_seek(jc, target, _SEEK[action])
    if action in ("stop", "close"):
        ctx.jellyfin_playing_title = None
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{target['Id']}/Playing/{_CONTROL[action]}")
    except Exception as e:
        return ToolResult(output="", error=f"control failed: {e}")
    return ToolResult(output=f"{action} sent.") if r.is_success else ToolResult(output="", error=f"control failed: HTTP {r.status_code}")


async def _rest_general_command(jc, session_id, name) -> ToolResult:
    """A Jellyfin GeneralCommand (e.g. VolumeUp/VolumeDown) against a native, controllable client."""
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session_id}/Command", json={"Name": name})
    except Exception as e:
        return ToolResult(output="", error=f"control failed: {e}")
    if not r.is_success:
        return ToolResult(output="", error=f"control failed: HTTP {r.status_code}")
    return ToolResult(output="Turned it up." if name == "VolumeUp" else "Turned it down.")


async def _rest_seek(jc, session, secs) -> ToolResult:
    """Relative seek on a native client: read PositionTicks, add ±secs (1s = 10,000,000 ticks), Seek to it."""
    pos = (session.get("PlayState") or {}).get("PositionTicks") or 0
    new = max(0, int(pos) + secs * 10_000_000)
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session['Id']}/Playing/Seek", params={"seekPositionTicks": new})
    except Exception as e:
        return ToolResult(output="", error=f"seek failed: {e}")
    if not r.is_success:
        return ToolResult(output="", error=f"seek failed: HTTP {r.status_code}")
    return ToolResult(output=f"Skipped ahead {secs} seconds." if secs > 0
                      else f"Skipped back {abs(secs)} seconds.")


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
        # Movies open full screen on the configured movie screen (default DP-1). Best-effort: if it
        # doesn't take, the user can still ask for full screen, and can leave it by asking. Fold the
        # outcome into the result so the spoken confirmation is grounded — narrating a fullscreen we
        # never performed was the honesty miss this replaces.
        if getattr(getattr(ctx.config, "desktop", None), "auto_fullscreen_movie", True):
            await asyncio.sleep(1.5)   # let the player view mount so the fullscreen gesture lands
            fs = await _enter_fullscreen(ctx)
            if fs.success and fs.output:
                result = ToolResult(output=f"{result.output} {fs.output}")
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

# A page.evaluate() with no timeout can hang for as long as the renderer is unresponsive — e.g. right
# after page.close(), or when closing another app moved the default audio sink and the page is wedged.
# media_state and the speech-duck call these on the *single shared* voice event loop, so one hung eval
# would freeze the whole brain. Cap every eval so it can't: on timeout OR error, return the safe default.
_EVAL_TIMEOUT = 2.0


async def _page_eval(page, script: str, arg=None, *, default=None, timeout: float | None = None):
    # Resolve the timeout at call time (not bound as a default) so it stays tunable/monkeypatchable.
    try:
        return await asyncio.wait_for(page.evaluate(script, arg), timeout=timeout or _EVAL_TIMEOUT)
    except Exception:
        return default


async def _video_paused(page) -> bool | None:
    return await _page_eval(
        page, "() => { const v = document.querySelector('video'); return v ? v.paused : null; }")


async def _video_volume(page) -> float | None:
    return await _page_eval(
        page, "() => { const v = document.querySelector('video'); return v ? v.volume : null; }")


async def _set_video_volume(page, vol: float) -> bool:
    # The JS returns true on success so a timeout/error (default False) is distinguishable from a real set.
    ok = await _page_eval(
        page,
        "(vol) => { const v = document.querySelector('video'); if (v) { v.volume = vol; return true; } return false; }",
        vol, default=False)
    return bool(ok)


async def _browser_control(ctx, page, action) -> ToolResult:
    try:
        if action == "stop":
            # Reliably pause (video.pause() needs no user gesture, unlike resume) and drop the player
            # out of fullscreen — but keep the window open. Escape alone (the old behavior) didn't
            # actually halt playback.
            await _page_eval(page, "() => { const v = document.querySelector('video'); if (v) v.pause(); }")
            await page.keyboard.press("Escape")
            ctx.jellyfin_paused = True
            return ToolResult(output="Paused the movie and came out of fullscreen.")
        if action == "close":
            closed_title = (getattr(ctx, "jellyfin_playing_title", None) or "")
            try:
                await page.close()
            except Exception:
                pass
            ctx.jellyfin_playing_page = None
            ctx.jellyfin_paused = False
            ctx.jellyfin_playing_title = None
            # Honesty: I only closed the window I opened. If a DIFFERENT (unowned) Jellyfin session is
            # still playing, say so rather than implying everything stopped (a real live miss — Rob heard
            # "closed" while a separate Chrome kept playing).
            if await _other_session_playing(ctx.config.jellyfin, exclude_title=closed_title):
                return ToolResult(output="I closed the window I opened, but another Jellyfin is still playing.")
            return ToolResult(output="Closed the movie window.")
        if action in ("volume_up", "volume_down"):
            # Drive the browser's PipeWire sink-input when it can be identified (robust — attenuates the
            # real audio even if the web player ignores <video>.volume), else fall back to <video>.volume.
            # adjust_movie_volume also keeps any active duck's restore prior in step with the manual change.
            from gabagent.voice.ducking import adjust_movie_volume
            ok = await adjust_movie_volume(ctx, page, up=(action == "volume_up"))
            if not ok:
                return ToolResult(output="", error="I couldn't read the movie volume.")
            return ToolResult(output="Turned the movie up." if action == "volume_up" else "Turned the movie down.")
        if action in _SEEK or action == "next":
            # A movie has no "next track", so bare "next"/"skip" means skip AHEAD (the live intent —
            # "can you skip?" used to hard-error "nothing to skip to"). forward/back carry an explicit sign.
            secs = _SEEK["forward"] if action == "next" else _SEEK[action]
            await _page_eval(
                page,
                "(s) => { const v = document.querySelector('video');"
                " if (v && !isNaN(v.duration)) v.currentTime = Math.max(0, Math.min(v.duration, v.currentTime + s)); }",
                secs)
            return ToolResult(output=f"Skipped ahead {secs} seconds." if secs > 0
                              else f"Skipped back {abs(secs)} seconds.")
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
