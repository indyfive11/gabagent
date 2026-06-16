"""First-party Jellyfin provider: search/control via REST, play-on-screen via the persistent
browser. Uses code backends (PyBackend/BrowserBackend) — so it ships trusted (not attested),
unlike third-party declarative skills.
"""
from __future__ import annotations
import asyncio
import json
import re
from typing import TYPE_CHECKING

import httpx

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, Slot, Detect, PyBackend, BrowserBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_CONTROL = {"pause": "Pause", "resume": "Unpause", "stop": "Stop", "next": "NextTrack"}
_REMOTE_VOLUME = {"volume_up": "VolumeUp", "volume_down": "VolumeDown"}  # GeneralCommand for native clients
_BROWSER_ONLY = {"close"}                                # only meaningful on the page we own
_SEEK = {"forward": 30, "back": -30}                     # ±seconds; owned page via currentTime, REST via ticks
_SEEK_TO = "seek_to"                                     # ABSOLUTE seek to `position` seconds from the start
_SPECIAL = {"fullscreen", "exit_fullscreen"}             # handled directly (two fullscreen layers)
_ACTIONS = set(_CONTROL) | set(_REMOTE_VOLUME) | _BROWSER_ONLY | set(_SEEK) | {_SEEK_TO} | _SPECIAL
_TICKS = 10_000_000                                      # Jellyfin position unit: 1 second = 10,000,000 ticks
# Actions whose target is "whatever is PLAYING" — so an owned-but-PAUSED page must not swallow them when a
# DIFFERENT session is the one actually going (the live miss Rob hit 2026-06-14). `resume` is excluded: it
# targets the PAUSED movie, which is the owned page; `close`/fullscreen are owned-only and handled separately.
_DIVERTABLE = set(_SEEK) | set(_REMOTE_VOLUME) | {"pause", "stop", "next", _SEEK_TO}
_PLAY_POLL_TRIES = 24   # ~12s waiting for the opened web client to register a session


def _coerce_secs(position) -> int | None:
    """A spoken position arrives as a number of SECONDS from the start (the model converts '1h20m' → 4800).
    Coerce defensively (str/float/None) to a non-negative int; None if absent/unparseable."""
    if position is None or position == "":
        return None
    try:
        return max(0, int(float(position)))
    except (TypeError, ValueError):
        return None


def _fmt_hms(secs: int) -> str:
    """A spoken time phrase: 4800 → '1 hour 20 minutes', 2700 → '45 minutes', 150 → '2 minutes 30 seconds'."""
    secs = max(0, int(secs))
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    parts = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s and not h:                                   # drop trailing seconds once we're past an hour
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    return " ".join(parts) or "the start"


# The wake vocative is the assistant's name; a leading/trailing "Aria"/"Arya" in a play/search request is the
# user addressing the assistant, NOT part of the movie title (live: "play the movie Aria" → searched "Aria").
# STT renders the name several ways in live transcripts — "Aria", "Arya", clipped "Ari", and spelled "R.A."
# (e.g. "Hey R.A., pause the movie"); cover them all. Longest forms first so "Aria" wins over "Ari".
_VOCATIVE_ALT = r"(?:aria|arya|ari|r\.\s*a\.?)"
_VOCATIVE_RE = re.compile(
    rf"^(?:hey\s+|ok(?:ay)?\s+)?{_VOCATIVE_ALT}[\s,]+|[\s,]+{_VOCATIVE_ALT}[\s,.!?]*$", re.I
)


def _strip_vocative(text) -> str:
    """Drop a leading/trailing wake vocative ('Aria'/'Arya', optional 'hey/ok') so it isn't taken as part of
    a movie title. Strips repeatedly (both ends), and only when something else remains — a bare 'Aria' is
    left as-is for the caller to handle (it carries no title)."""
    s = (text or "").strip()
    while True:
        stripped = _VOCATIVE_RE.sub(" ", s).strip(" ,.!?")
        if stripped == s or not stripped:
            break
        s = stripped
    return s


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
                    Slot("position", "number", False,
                         description="optional — START position in SECONDS from the beginning (convert spoken "
                         "times: '1 hour 20 minutes' → 4800, 'forty-five minutes in' → 2700). Omit to start "
                         "from the beginning / resume point."),
                ],
                examples=["play that one", "play the first movie",
                          "play it starting at an hour and ten minutes", "start the movie twenty minutes in"],
            ),
            Command(
                id="jellyfin.control", domain="media", tier=1, featured=True,
                summary="Control the movie — pause, resume, stop, fast-forward/rewind ±30s, jump to a specific time, enter or leave full screen, close the window, or turn the movie volume up/down",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:control"),
                params=[Slot("action", "enum", True,
                             enum=("pause", "resume", "stop", "next", "forward", "back", "seek_to",
                                   "fullscreen", "exit_fullscreen", "close", "volume_up", "volume_down"),
                             description="required — one of pause/resume/stop/next/forward/back/seek_to/"
                             "fullscreen/exit_fullscreen/close/volume_up/volume_down (forward/back/next = "
                             "skip ±30s, seek_to = jump to an ABSOLUTE time given in `position`, "
                             "fullscreen = put the movie full screen, exit_fullscreen = leave it)"),
                        Slot("position", "number", False,
                             description="for seek_to only — the target time in SECONDS from the start "
                             "(convert spoken times: 'forty-five minutes' → 2700, 'an hour in' → 3600)")],
                examples=["pause", "resume", "stop the movie", "fast forward", "skip ahead",
                          "rewind", "go back thirty seconds", "jump to the forty-five minute mark",
                          "skip to one hour ten", "go to two hours in", "full screen the movie", "go fullscreen",
                          "leave full screen", "exit fullscreen", "get out of full screen",
                          "close the movie window", "turn up the movie", "louder on the movie", "movie volume down"],
            ),
            Command(
                id="jellyfin.now_playing", domain="media", tier=1,
                summary="What Jellyfin is playing on EVERY device — title, playing/paused, position, and "
                        "which device. Use this to check playback anywhere (other rooms/TVs), and before "
                        "saying nothing is playing.",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:now_playing"),
                examples=["what are we watching", "what movie is this", "what's playing on Jellyfin",
                          "is the movie still playing", "is anything playing in the bedroom",
                          "what's playing on my other devices", "is the TV still going",
                          "did the movie actually start"],
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


def _fmt_ticks(ticks) -> str:
    """Jellyfin position/runtime ticks (100ns units) → 'h:mm:ss' or 'm:ss'."""
    s = int((ticks or 0) // 10_000_000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


_NOW_PLAYING_CAP = 8   # plenty for a household; cap so a runaway session list can't bloat the model's context


async def now_playing(ctx) -> ToolResult:
    """A compact summary of EVERY Jellyfin session across ALL devices — title, playing/paused, position,
    and which device — so the brain can answer "is the movie still on in the bedroom?" not just "what am I
    watching here". On-demand only: the always-on media_state stays LOCAL-scoped to avoid cross-device
    flap, and this summary is a few short lines, so it never bloats the model's context."""
    jc = ctx.config.jellyfin
    if not jc.enabled or not jc.api_key:
        return ToolResult(output="", error="Jellyfin isn't set up.")
    lines: list[str] = []
    owned_title = (getattr(ctx, "jellyfin_playing_title", "") or "").strip()
    # The movie in the window WE own reads its REAL <video> state (more accurate than the session API,
    # which reports "playing" even when the element is autoplay-paused on the library).
    page = _live_jellyfin_page(ctx)
    if page is not None:
        paused = await _video_paused(page)
        lines.append(f"Here (this machine): {'Paused' if paused is True else 'Playing'} — "
                     f"{owned_title or 'the movie'}")
    try:
        sessions = await _sessions(jc)
    except Exception:
        return ToolResult(output="", error="I couldn't reach Jellyfin.")
    others = []
    for s in sessions:
        item = s.get("NowPlayingItem")
        if not item or not item.get("Name"):
            continue
        # Skip the session that IS our owned window (already reported above from the live element).
        if page is not None and (item["Name"]).strip().lower() == owned_title.lower():
            continue
        ps = s.get("PlayState") or {}
        pos, dur = _fmt_ticks(ps.get("PositionTicks")), _fmt_ticks(item.get("RunTimeTicks"))
        at = f" ({pos} of {dur})" if dur != "0:00" else (f" ({pos})" if pos != "0:00" else "")
        where = s.get("DeviceName") or s.get("Client") or "a device"
        others.append(f"{where}: {'Paused' if ps.get('IsPaused') else 'Playing'} — {item['Name']}{at}")
    lines.extend(others[:_NOW_PLAYING_CAP])
    if len(others) > _NOW_PLAYING_CAP:                 # no silent truncation
        lines.append(f"(+{len(others) - _NOW_PLAYING_CAP} more session(s) not shown)")
    if not lines:
        return ToolResult(output="Nothing is playing in Jellyfin right now, on any device.")
    return ToolResult(output="\n".join(lines))


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
    if query:
        # "play the movie Aria" carries a trailing wake vocative, not a title — strip it so the search
        # term is the real title (a bare vocative collapses to no query → a normal browse, not "Aria").
        query = _strip_vocative(query) or None
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
    from gabagent.commands.providers.desktop import (
        fullscreen as _kwin_fullscreen, to_movie_screen, _movie_window_hint)
    from gabagent.voice.debuglog import dlog
    # The caption we'll target. `kwin_ok` only reports that the KWin script RAN (KWin scripting gives no
    # readable match result), so log the hint too: a movie title means we aimed at the self-named movie
    # window; an empty/server-name hint is the signature of the wrong-window bug this path replaced.
    hint = await _movie_window_hint(ctx)
    moved = await to_movie_screen(ctx)   # put it on the configured movie screen (DP-1) first, if it's connected
    where = f" on {moved}" if moved else ""
    page = _live_jellyfin_page(ctx)
    if page is not None:
        try:
            await page.keyboard.press("f")
        except Exception:
            pass
        kwin = await _kwin_fullscreen(ctx)   # also raise the window to KWin fullscreen
        dlog(ctx, "fullscreen", path="owned", moved=moved, kwin_ok=kwin.success, hint=hint)
        return ToolResult(output=f"Set the movie to full screen{where}.")
    res = await _kwin_fullscreen(ctx)
    dlog(ctx, "fullscreen", path="unowned", moved=moved, kwin_ok=res.success, hint=hint)
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


def _is_web_client(session: dict) -> bool:
    """True if this session is a Jellyfin WEB client. Web clients ignore remote-control REST commands
    (they 204 and do nothing — the upstream issue noted below) YET still report SupportsRemoteControl:true,
    so the `Client` string is the only reliable native-vs-web discriminator. We can drive a web movie only
    if WE launched it (the owned Playwright page); a web movie on another device is uncontrollable."""
    return "web" in (session.get("Client") or "").lower()


def _session_playing(s: dict) -> bool:
    return bool(s.get("NowPlayingItem")) and not (s.get("PlayState") or {}).get("IsPaused")


def _other_playing_session(ctx, sessions: list[dict]) -> dict | None:
    """The actively-playing session that is NOT the movie we own (matched by title — our own web session
    can linger in /Sessions for a beat after we pause/close). Prefers a native client over a web one, so a
    controllable target wins when both are present. None if nothing else is playing."""
    owned = (getattr(ctx, "jellyfin_playing_title", "") or "").strip().lower()
    playing = [s for s in sessions if _session_playing(s)
               and (s.get("NowPlayingItem", {}).get("Name") or "").strip().lower() != owned]
    if not playing:
        return None
    return next((s for s in playing if not _is_web_client(s)), playing[0])


async def _rest_control(ctx, jc, session: dict, action: str, secs: int | None = None) -> ToolResult:
    """Drive a non-owned Jellyfin session over the Sessions REST API. Honest about web clients, which
    accept the command (HTTP 204) but do nothing — so we refuse rather than falsely claim success."""
    if _is_web_client(session):
        title = (session.get("NowPlayingItem", {}).get("Name") or "that movie")
        device = session.get("DeviceName") or session.get("Client") or "another device"
        return ToolResult(output="", error=f'"{title}" is playing in a web browser on {device} — Jellyfin '
                          "web players can't be remote-controlled. I can only control a movie I opened here.")
    if action in _REMOTE_VOLUME:
        return await _rest_general_command(jc, session["Id"], _REMOTE_VOLUME[action])
    if action == _SEEK_TO:
        return await _rest_seek_to(jc, session, secs or 0)
    if action in _SEEK:
        return await _rest_seek(jc, session, _SEEK[action])
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session['Id']}/Playing/{_CONTROL[action]}")
    except Exception as e:
        return ToolResult(output="", error=f"control failed: {e}")
    return ToolResult(output=f"{action} sent.") if r.is_success else ToolResult(output="", error=f"control failed: HTTP {r.status_code}")


async def control(ctx, action="", position=None) -> ToolResult:
    jc = ctx.config.jellyfin
    if action not in _ACTIONS:
        return ToolResult(output="", error=f"unknown action: {action}")
    if action == "fullscreen":
        return await _enter_fullscreen(ctx)
    if action == "exit_fullscreen":
        return await _exit_fullscreen(ctx)
    secs = _coerce_secs(position)
    if action == _SEEK_TO and secs is None:
        return ToolResult(output="", error="Tell me a time to jump to — like 'skip to forty-five minutes'.")
    # The Jellyfin web client ignores remote-control API commands (returns 204, does nothing — a
    # known upstream issue), so when WE launched the movie in our own browser, control it through
    # the page instead. Native/controllable clients still go through the Sessions API below.
    page = _live_jellyfin_page(ctx)
    # `close` only ever means "close the window I opened" — never a session on another device.
    if action == "close":
        if page is not None:
            return await _browser_control(ctx, page, action)
        return ToolResult(output="", error="I can only do that to a movie I opened in the browser.")
    if page is not None:
        # Don't let an owned-but-PAUSED page swallow a pause/stop/seek/volume meant for a different
        # session that's actually playing — route to that session instead. resume/owned-playing/no-other
        # all fall through to the owned page (its proven path).
        if action in _DIVERTABLE and (await _video_paused(page)) is not False:
            target = _other_playing_session(ctx, await _sessions(jc))
            if target is not None:
                return await _rest_control(ctx, jc, target, action, secs)
        return await _browser_control(ctx, page, action, secs)
    # No owned page → drive a native/controllable Jellyfin client over REST (prefer what's actually playing).
    sessions = await _sessions(jc)
    target = _other_playing_session(ctx, sessions) \
        or next((s for s in sessions if s.get("NowPlayingItem")), None) \
        or next((s for s in sessions if s.get("SupportsRemoteControl")), None)
    if not target:
        return ToolResult(output="", error="No active playback session to control.")
    if action in ("stop", "close"):
        ctx.jellyfin_playing_title = None
    return await _rest_control(ctx, jc, target, action, secs)


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


async def _rest_seek_to(jc, session, secs: int) -> ToolResult:
    """Absolute seek on a native client: jump to `secs` from the start (seconds → ticks)."""
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session['Id']}/Playing/Seek",
                             params={"seekPositionTicks": max(0, int(secs)) * _TICKS})
    except Exception as e:
        return ToolResult(output="", error=f"seek failed: {e}")
    if not r.is_success:
        return ToolResult(output="", error=f"seek failed: HTTP {r.status_code}")
    return ToolResult(output=f"Jumped to {_fmt_hms(secs)}.")


async def play(ctx, item_id="", title="", position=None) -> ToolResult:
    # `title` is carried for the spoken confirmation only; playback uses item_id.
    from gabagent.voice import events
    jc = ctx.config.jellyfin
    if not item_id:
        return ToolResult(output="", error="no item to play")
    start_secs = _coerce_secs(position)            # optional "start N seconds in"
    emit = getattr(ctx, "voice_emit", None)

    # ALWAYS open OUR controllable window. We deliberately do NOT branch on the Jellyfin /Sessions list to
    # offer "play there?": that list usually reflects API sessions on OTHER devices (a movie playing in
    # another room), not a real local client we could hand off to here — so the old confirm fired on
    # phantom/remote clients and was misleading. A web client also can't be REST-controlled anyway. So a play
    # here just opens the window we own and can drive (via the <video> element), with no narration. Routing a
    # play to another device ("play it on the bedroom TV") is the deferred whole-home roadmap, not this path.
    if emit:
        await emit(events.status("Opening the player…"))
    res = await _play_in_browser(ctx, jc, item_id, title, start_secs)
    if res.success and title:
        ctx.jellyfin_playing_title = title
    return res


async def _play_to_session(jc, session_id, item_id, label, start_secs: int | None = None) -> ToolResult:
    params = {"itemIds": item_id, "playCommand": "PlayNow"}
    if start_secs:
        params["startPositionTicks"] = int(start_secs) * _TICKS     # begin N seconds in
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session_id}/Playing", params=params)
    except Exception as e:
        return ToolResult(output="", error=f"play failed: {e}")
    if not r.is_success:
        return ToolResult(output="", error=f"play failed: HTTP {r.status_code}")
    at = f" from {_fmt_hms(start_secs)}" if start_secs else ""
    return ToolResult(output=f"Playing on {label}{at}.")


async def _play_in_browser(ctx, jc, item_id, title="", start_secs: int | None = None) -> ToolResult:
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
        # Self-name the window NOW (well before the 1.5s fullscreen settle) so KWin can target THIS
        # browser window by its caption. Jellyfin's own document.title reads the server name (the
        # hostname) for a beat after play, which collided with Conky's caption and sent the move +
        # fullscreen to the wrong window. Stamping early lets the title propagate to the WM caption
        # before we act, and disambiguates our window from any other Chrome the user has open.
        if title:
            ctx.jellyfin_playing_title = title
            try:
                await page.evaluate("(t) => { document.title = t; }", title)
            except Exception:
                pass
        # Start at full volume — the persistent browser reuses the same <video> element, so a duck
        # that got stranded low in a prior session shouldn't leave a freshly-played movie quiet.
        await _set_video_volume(page, 1.0)
        # Verify the movie ACTUALLY started instead of trusting the session API's "playing" (which lies
        # when PlayNow didn't navigate to the player, or the browser autoplay-paused the element — the
        # "library came up full screen but the movie didn't play" bug). Nudge a paused player, and emit
        # a `playback` event so a verify run sees the real <video> state, not the server's claim.
        from gabagent.voice.debuglog import dlog
        pb = await _await_playback(page)
        dlog(ctx, "playback", has_video=pb["has_video"], paused=pb["paused"],
             started=pb["started"], nudged=pb["nudged"], t=pb["t"], ready=pb["ready"],
             muted=pb["muted"], vol=pb["vol"])
        # Make the sink-input audible at two layers the <video> reset can't reach: clear a stranded MUTE flag,
        # AND un-strand a LOW volume — PipeWire stream-restore replays the prior movie's ducked level (~18%)
        # onto the new stream, so a fresh movie plays quiet at full <video>.volume. lift_volume=True raises it
        # to the start baseline (movie_start_volume). Runs once playback is up.
        from gabagent.voice.ducking import ensure_jellyfin_sink_audible
        await ensure_jellyfin_sink_audible(ctx, lift_volume=True)
        if not pb["started"]:
            # Don't claim it's playing when the element says otherwise.
            result = ToolResult(output=(
                "I opened and full-screened the window, but the movie isn't actually playing yet — say "
                "'play' and I'll start it." if pb["has_video"] else
                "I opened the Jellyfin window but the player didn't come up — ask me to play again."))
        # Start-at-position: once the element is genuinely playing, jump to the requested point. We seek the
        # <video> directly (deterministic for the owned browser) rather than trust the web client to honor a
        # PlayNow startPositionTicks, and fold it into the spoken line so the confirmation stays grounded.
        elif start_secs:
            if await _browser_seek_to(page, start_secs):
                base = (result.output or "Playing.").rstrip(".")
                result = ToolResult(output=f"{base} — starting at {_fmt_hms(start_secs)}.")
        # Fullscreen only once a player is up (never fullscreen the bare library). Append the fullscreen
        # line only when playback is real, so the spoken confirmation stays grounded.
        if pb["has_video"] and getattr(getattr(ctx.config, "desktop", None), "auto_fullscreen_movie", True):
            fs = await _enter_fullscreen(ctx)
            if fs.success and fs.output and pb["started"]:
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


# How long to wait for the web client to ACTUALLY start the <video> after a PlayNow command. The
# session API reports "playing" the instant the web client accepts the command, but the real element
# can still be on the library (PlayNow never navigated) or held paused by the browser's autoplay
# policy (a fresh window has no user-gesture grant, and v.play() from evaluate can't earn one) — so we
# verify the element itself and nudge a paused player. Constants (not literals) so tests stay fast.
_PLAYBACK_POLL_TRIES = 12       # ~6s
_PLAYBACK_POLL_INTERVAL = 0.5
_JS_VIDEO_STATE = (
    "() => { const v = document.querySelector('video');"
    " return { has_video: !!v, paused: v ? v.paused : null,"
    " t: v ? v.currentTime : null, ready: v ? v.readyState : null,"
    " muted: v ? v.muted : null, vol: v ? v.volume : null }; }"
)


async def _await_playback(page) -> dict:
    """Wait for the owned page's <video> to be genuinely playing; if the player mounted but autoplay
    left it paused, nudge it ONCE with a trusted key gesture (Space toggles play/pause in the Jellyfin
    web player, and a real keypress satisfies the autoplay user-gesture requirement). Returns the final
    {has_video, paused, t, ready, started, nudged} for logging and an honest spoken line."""
    nudged = False
    st: dict = {}
    for _ in range(_PLAYBACK_POLL_TRIES):
        st = await _page_eval(page, _JS_VIDEO_STATE, default=None) or {}
        if st.get("has_video"):
            if st.get("paused") is False:
                break                                    # genuinely playing — done
            if not nudged:                               # mounted but paused → autoplay block; nudge
                try:
                    await page.keyboard.press("Space")
                except Exception:
                    pass
                nudged = True
        await asyncio.sleep(_PLAYBACK_POLL_INTERVAL)
    started = bool(st.get("has_video") and st.get("paused") is False)
    return {"has_video": bool(st.get("has_video")), "paused": st.get("paused"),
            "t": st.get("t"), "ready": st.get("ready"), "started": started, "nudged": nudged,
            "muted": st.get("muted"), "vol": st.get("vol")}


async def _video_volume(page) -> float | None:
    return await _page_eval(
        page, "() => { const v = document.querySelector('video'); return v ? v.volume : null; }")


async def _set_video_volume(page, vol: float) -> bool:
    # Setting a volume means "make the movie audible at this level", so also clear the element's muted
    # flag. A browser autoplay/resume can leave <video>.muted=true (autoplay policy), and a muted element
    # stays silent no matter the volume OR the PipeWire sink level — that's the movie-stuck-muted-on-resume
    # bug, where a sink-level "turn it up" couldn't recover the audio. The JS returns true on success so a
    # timeout/error (default False) is distinguishable from a real set.
    ok = await _page_eval(
        page,
        "(vol) => { const v = document.querySelector('video');"
        " if (v) { v.muted = false; v.volume = vol; return true; } return false; }",
        vol, default=False)
    return bool(ok)


async def _ensure_video_audible(ctx, page, where="") -> None:
    """Clear a stranded mute on the owned <video> so audio actually returns on resume/play. A browser
    autoplay/resume can leave the element muted (or at volume 0) while the PipeWire sink sits at a normal
    level — the movie then plays silently and a sink-level "turn it up" can't recover it (the
    muted-on-resume bug Rob hit). Best-effort and instrumented: logs before/after so a silent resume is
    provable. Unmutes; lifts a 0/unknown volume to full; leaves a real user-set level alone."""
    try:
        st = await _page_eval(page, _JS_VIDEO_STATE, default=None) or {}
    except Exception:
        st = {}
    if not st.get("has_video"):
        return
    was_muted, was_vol = st.get("muted"), st.get("vol")
    target = 1.0 if (was_vol is None or was_vol <= 0.0) else was_vol
    ok = await _set_video_volume(page, target)            # also clears muted
    try:
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "video_audible", where=where, was_muted=was_muted, was_vol=was_vol,
             set_to=target, ok=ok)
    except Exception:
        pass


async def _browser_seek_to(page, secs: int) -> bool:
    """Absolute seek on the owned <video>: jump to `secs` from the start, clamped to the duration.
    Returns true only when a real <video> was found and set (a timeout/no-element reads False)."""
    return bool(await _page_eval(
        page,
        "(s) => { const v = document.querySelector('video');"
        " if (v && !isNaN(v.duration)) { v.currentTime = Math.max(0, Math.min(v.duration, s)); return true; }"
        " return false; }",
        max(0, int(secs)), default=False))


async def _browser_control(ctx, page, action, position: int | None = None) -> ToolResult:
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
            # Per the user's SOP (config jellyfin.announce_other_sessions, default OFF): do NOT volunteer that
            # a DIFFERENT (unowned/other-device) Jellyfin session is still playing — close just reports what it
            # did. The on-demand jellyfin.now_playing is the explicit "is anything playing elsewhere?" path.
            # (Opt back in to restore the old "…but another is still playing" honesty notice.)
            if getattr(ctx.config.jellyfin, "announce_other_sessions", False) \
                    and await _other_session_playing(ctx.config.jellyfin, exclude_title=closed_title):
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
        if action == _SEEK_TO:
            if position is None:
                return ToolResult(output="", error="Tell me a time to jump to.")
            if not await _browser_seek_to(page, position):
                return ToolResult(output="", error="I couldn't jump there — the movie isn't ready yet.")
            return ToolResult(output=f"Jumped to {_fmt_hms(position)}.")
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
            from gabagent.voice.ducking import ensure_jellyfin_sink_audible
            if cur is False:
                ctx.jellyfin_paused = False
                await _ensure_video_audible(ctx, page, where="resume_already")
                await ensure_jellyfin_sink_audible(ctx)
                return ToolResult(output="It's already playing.")
            await page.keyboard.press("Space")
            ctx.jellyfin_paused = False
            # A resume can come back muted/silent (autoplay policy) while the sink is fine — unmute the
            # element so audio actually returns, instead of leaving Rob with a silent movie. Also clear a
            # stranded sink-input mute (a separate layer the element unmute can't reach).
            await _ensure_video_audible(ctx, page, where="resume")
            await ensure_jellyfin_sink_audible(ctx)
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
