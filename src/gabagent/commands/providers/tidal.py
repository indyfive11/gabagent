"""First-party TIDAL provider via a local Mopidy + mopidy-tidal server.

Mopidy exposes an HTTP JSON-RPC API (http://localhost:6680/mopidy/rpc). This skill drives the
full voice flow — search the TIDAL library, then clear/queue/play a result — plus transport
(pause, resume, next, previous, stop) and "what's playing". It uses code backends (PyBackend),
so it ships trusted (not attested), like the Jellyfin provider.

Setup (one-time, user side): install Mopidy + Mopidy-Tidal, authorize TIDAL (OAuth), and run the
server. See SETUP_TIDAL.md.
"""
from __future__ import annotations
import json
from typing import TYPE_CHECKING

import httpx

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, Slot, Detect, PyBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_RPC_TIMEOUT = 15.0


class TidalProvider:
    id = "tidal"

    async def detect(self, ctx: AgentContext) -> bool:
        tc = getattr(ctx.config, "tidal", None)
        if not tc or not tc.enabled:
            return False
        try:
            res = await _rpc(tc, "core.get_version", timeout=2.0)
            return res is not None
        except Exception:
            return False

    def commands(self, ctx: AgentContext) -> list[Command]:
        ref = "gabagent.commands.providers.tidal:"
        return [
            Command(
                id="tidal.search", domain="media", tier=1, structured=True,
                summary="Search TIDAL for music by name, artist, or album",
                backend=PyBackend(ref=ref + "search"), detect=Detect(),
                params=[Slot("query", "string", True, description="what to search for, e.g. 'Miles Davis'")],
                examples=["find some Radiohead on tidal", "search tidal for jazz"],
            ),
            Command(
                id="tidal.play", domain="media", tier=1,
                summary="Play music on TIDAL — searches and plays, or resumes if no query is given",
                backend=PyBackend(ref=ref + "play"),
                params=[
                    Slot("query", "string", False, description="what to play, e.g. 'Kind of Blue'"),
                    Slot("uri", "string", False, description="a tidal: URI from tidal.search to play exactly"),
                ],
                examples=["play some Miles Davis on tidal", "play kind of blue", "play music"],
            ),
            Command(id="tidal.pause", domain="media", tier=1, summary="Pause TIDAL playback",
                    backend=PyBackend(ref=ref + "pause"), examples=["pause the music", "pause tidal"]),
            Command(id="tidal.resume", domain="media", tier=1, summary="Resume TIDAL playback",
                    backend=PyBackend(ref=ref + "resume"), examples=["resume", "keep playing"]),
            Command(id="tidal.next", domain="media", tier=1, summary="Skip to the next track",
                    backend=PyBackend(ref=ref + "next"), examples=["next song", "skip"]),
            Command(id="tidal.previous", domain="media", tier=1, summary="Go to the previous track",
                    backend=PyBackend(ref=ref + "previous"), examples=["previous track", "go back a song"]),
            Command(id="tidal.stop", domain="media", tier=1, summary="Stop TIDAL playback",
                    backend=PyBackend(ref=ref + "stop"), examples=["stop the music"]),
            Command(id="tidal.now_playing", domain="media", tier=1, structured=True,
                    summary="What's playing on TIDAL right now",
                    backend=PyBackend(ref=ref + "now_playing"), examples=["what's playing", "what song is this"]),
        ]


PROVIDER = TidalProvider()


# -- JSON-RPC plumbing -----------------------------------------------------

async def _rpc(tc, method: str, params: dict | None = None, timeout: float = _RPC_TIMEOUT):
    """Call a Mopidy JSON-RPC method. Raises on transport/RPC error; returns `result`."""
    payload: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(tc.rpc_url, json=payload)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"].get("message", "RPC error")))
    return data.get("result") if isinstance(data, dict) else None


def _artist_names(track: dict) -> str:
    return ", ".join(a.get("name", "") for a in (track.get("artists") or []) if a.get("name"))


def _flatten_tracks(search_results) -> list[dict]:
    out: list[dict] = []
    for res in search_results or []:
        for t in res.get("tracks") or []:
            out.append({
                "uri": t.get("uri"),
                "title": t.get("name"),
                "artist": _artist_names(t),
                "album": (t.get("album") or {}).get("name", ""),
            })
    return out


# -- backend callables -----------------------------------------------------

async def search(ctx, query="", limit=8) -> ToolResult:
    tc = ctx.config.tidal
    if not query:
        return ToolResult(output="", error="nothing to search for")
    try:
        results = await _rpc(tc, "core.library.search", {"query": {"any": [query]}})
    except Exception as e:
        return ToolResult(output="", error=f"TIDAL search failed: {e}")
    tracks = [t for t in _flatten_tracks(results) if t["uri"]][:limit]
    return ToolResult(output=json.dumps(tracks))


async def play(ctx, query="", uri="") -> ToolResult:
    tc = ctx.config.tidal
    label = ""
    if not uri and query:
        try:
            results = await _rpc(tc, "core.library.search", {"query": {"any": [query]}})
        except Exception as e:
            return ToolResult(output="", error=f"TIDAL search failed: {e}")
        tracks = [t for t in _flatten_tracks(results) if t["uri"]]
        if not tracks:
            return ToolResult(output="", error=f"I couldn't find '{query}' on TIDAL.")
        uri = tracks[0]["uri"]
        label = tracks[0]["title"] + (f" by {tracks[0]['artist']}" if tracks[0]["artist"] else "")
    if not uri:
        # No query and no uri → just resume whatever is queued.
        try:
            await _rpc(tc, "core.playback.resume")
            return ToolResult(output="Resuming.")
        except Exception as e:
            return ToolResult(output="", error=f"couldn't resume: {e}")
    try:
        await _rpc(tc, "core.tracklist.clear")
        await _rpc(tc, "core.tracklist.add", {"uris": [uri]})
        await _rpc(tc, "core.playback.play")
    except Exception as e:
        return ToolResult(output="", error=f"couldn't play that: {e}")
    return ToolResult(output=f"Playing {label} on TIDAL." if label else "Playing on TIDAL.")


async def _transport(ctx, method: str, said: str) -> ToolResult:
    try:
        await _rpc(ctx.config.tidal, method)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't do that: {e}")
    return ToolResult(output=said)


async def pause(ctx) -> ToolResult:
    return await _transport(ctx, "core.playback.pause", "Paused.")


async def resume(ctx) -> ToolResult:
    return await _transport(ctx, "core.playback.resume", "Resuming.")


async def next(ctx) -> ToolResult:
    return await _transport(ctx, "core.playback.next", "Skipping ahead.")


async def previous(ctx) -> ToolResult:
    return await _transport(ctx, "core.playback.previous", "Going back.")


async def stop(ctx) -> ToolResult:
    return await _transport(ctx, "core.playback.stop", "Stopped.")


async def now_playing(ctx) -> ToolResult:
    try:
        track = await _rpc(ctx.config.tidal, "core.playback.get_current_track")
    except Exception as e:
        return ToolResult(output="", error=f"couldn't check: {e}")
    if not track:
        return ToolResult(output=json.dumps({"playing": False}))
    artist = _artist_names(track)
    return ToolResult(output=json.dumps({
        "playing": True, "title": track.get("name"), "artist": artist,
        "album": (track.get("album") or {}).get("name", ""),
    }))
