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
                id="jellyfin.search", domain="media", tier=1, structured=True,
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
                id="jellyfin.play", domain="media", tier=2, requires_confirm_surface=True,
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
                id="jellyfin.control", domain="media", tier=1,
                summary="Control playback: pause, resume, stop, or skip to next",
                backend=PyBackend(ref="gabagent.commands.providers.jellyfin:control"),
                params=[Slot("action", "enum", True, enum=("pause", "resume", "stop", "next"))],
                examples=["pause", "resume", "stop the movie"],
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
        "IncludeItemTypes": "Movie", "Recursive": "true",
        "Fields": "Genres,CommunityRating,ProductionYear", "SortBy": "Random", "Limit": "20",
    }
    mr = min_rating if min_rating is not None else jc.rating_threshold
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


async def control(ctx, action="") -> ToolResult:
    jc = ctx.config.jellyfin
    if action not in _CONTROL:
        return ToolResult(output="", error=f"unknown action: {action}")
    sessions = await _sessions(jc)
    target = next((s for s in sessions if s.get("NowPlayingItem")), None) \
        or next((s for s in sessions if s.get("SupportsRemoteControl")), None)
    if not target:
        return ToolResult(output="", error="No active playback session to control.")
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{target['Id']}/Playing/{_CONTROL[action]}")
    except Exception as e:
        return ToolResult(output="", error=f"control failed: {e}")
    return ToolResult(output=f"{action} sent.") if r.is_success else ToolResult(output="", error=f"control failed: HTTP {r.status_code}")


async def play(ctx, item_id="", title="") -> ToolResult:
    # `title` is carried for the spoken confirmation only; playback uses item_id.
    jc = ctx.config.jellyfin
    if not item_id:
        return ToolResult(output="", error="no item to play")
    emit = getattr(ctx, "voice_emit", None)
    vs = getattr(ctx, "voice_session", None)

    sessions = await _sessions(jc)
    controllable = [s for s in sessions if s.get("SupportsRemoteControl") and s.get("DeviceName")]

    # Ask-if-ambiguous: a client is already open — use it, or open a new window?
    if controllable and vs is not None and emit is not None:
        from gabagent.voice import events
        names = " or ".join(s.get("DeviceName", "a device") for s in controllable[:2])
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
            return await _play_to_session(jc, controllable[0]["Id"], item_id,
                                          controllable[0].get("DeviceName", "your client"))

    if emit:
        await emit(events.status("Opening the player…"))  # narrate before the slow launch
    return await _play_in_browser(ctx, jc, item_id)


async def _play_to_session(jc, session_id, item_id, label) -> ToolResult:
    try:
        async with _client(jc) as c:
            r = await c.post(f"/Sessions/{session_id}/Playing", params={"itemIds": item_id, "playCommand": "PlayNow"})
    except Exception as e:
        return ToolResult(output="", error=f"play failed: {e}")
    return ToolResult(output=f"Playing on {label}.") if r.is_success else ToolResult(output="", error=f"play failed: HTTP {r.status_code}")


async def _play_in_browser(ctx, jc, item_id) -> ToolResult:
    from gabagent.commands.browser import ensure_browser

    before = {s["Id"] for s in await _sessions(jc)}
    try:
        bctx = await ensure_browser(ctx, profile="jellyfin")
        page = bctx.pages[0] if bctx.pages else await bctx.new_page()
        await page.goto(jc.base_url.rstrip("/") + "/web/", wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't open the player: {e}")

    # Wait for the web client to register a new session, then command it.
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
        return ToolResult(output="I opened the Jellyfin window — please sign in there once, then ask me to play again.")
    return await _play_to_session(jc, session_id, item_id, "the new window")
