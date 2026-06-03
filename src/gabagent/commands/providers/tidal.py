"""First-party TIDAL provider via a local Mopidy + mopidy-tidal server.

Mopidy exposes an HTTP JSON-RPC API (http://localhost:6680/mopidy/rpc). This skill drives the
full voice flow — search the TIDAL library, then clear/queue/play a result — plus transport
(pause, resume, next, previous, stop) and "what's playing". It uses code backends (PyBackend),
so it ships trusted (not attested), like the Jellyfin provider.

Setup (one-time, user side): install Mopidy + Mopidy-Tidal, authorize TIDAL (OAuth), and run the
server. See SETUP_TIDAL.md.
"""
from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING

import httpx

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, Slot, Detect, PyBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_RPC_TIMEOUT = 30.0   # search and mix/album expand hit TIDAL's live API, not the local cache


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
                id="tidal.play", domain="media", tier=1, featured=True,
                summary="Play music on TIDAL — a track, or a whole album (album=true), or resume",
                backend=PyBackend(ref=ref + "play"),
                params=[
                    Slot("query", "string", False, description="what to play, e.g. 'Kind of Blue'"),
                    Slot("uri", "string", False, description="a tidal: URI from tidal.search to play exactly"),
                    Slot("album", "boolean", False,
                         description="true to queue the whole album instead of a single track"),
                ],
                examples=["play some Miles Davis on tidal", "play the album Dizzy Up the Girl",
                          "play kind of blue", "play music",
                          "play my Metallica playlist (uri from tidal.playlists)",
                          "play my New Arrivals mix (uri from tidal.recommendations)"],
            ),
            Command(
                id="tidal.playlists", domain="media", tier=1, structured=True, featured=True,
                summary="List your saved TIDAL playlists (play one by passing its uri to tidal.play)",
                backend=PyBackend(ref=ref + "playlists"),
                examples=["what playlists do I have", "show my tidal playlists", "list my playlists"],
            ),
            Command(
                id="tidal.recommendations", domain="media", tier=1, structured=True, featured=True,
                summary="Personalized TIDAL mixes & recommendations from your listening history "
                        "(play one by passing its uri to tidal.play)",
                backend=PyBackend(ref=ref + "recommendations"),
                examples=["what should I listen to", "play my recommendations", "my mixes",
                          "recommend something based on what I listen to"],
            ),
            Command(id="tidal.pause", domain="media", tier=1, featured=True, summary="Pause TIDAL playback",
                    backend=PyBackend(ref=ref + "pause"), examples=["pause the music", "pause tidal"]),
            Command(id="tidal.resume", domain="media", tier=1, summary="Resume TIDAL playback",
                    backend=PyBackend(ref=ref + "resume"), examples=["resume", "keep playing"]),
            Command(id="tidal.next", domain="media", tier=1, featured=True, summary="Skip to the next track",
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


def _is_timeout(e: Exception) -> bool:
    return isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)) or not str(e).strip()


def _human_err(e: Exception) -> str:
    """A non-empty, speakable error string — a timeout (or an exception with no message) becomes words,
    never a bare dangling colon in Aria's reply."""
    if _is_timeout(e):
        return "it timed out"
    return str(e).strip() or "it didn't respond"


async def _rpc_resilient(tc, method: str, params: dict | None = None, timeout: float = _RPC_TIMEOUT):
    """_rpc, but retry once on a timeout — TIDAL's first live-API call often warms a cache and the
    second succeeds (a direct browse then returns in ~0.5s)."""
    try:
        return await _rpc(tc, method, params, timeout)
    except Exception as e:
        if not _is_timeout(e):
            raise
        return await _rpc(tc, method, params, timeout)


def _artist_names(track: dict) -> str:
    return ", ".join(a.get("name", "") for a in (track.get("artists") or []) if a.get("name"))


def _flatten_tracks(search_results) -> list[dict]:
    out: list[dict] = []
    for res in search_results or []:
        for t in res.get("tracks") or []:
            out.append({
                "kind": "track",
                "uri": t.get("uri"),
                "title": t.get("name"),
                "artist": _artist_names(t),
                "album": (t.get("album") or {}).get("name", ""),
            })
    return out


def _flatten_albums(search_results) -> list[dict]:
    out: list[dict] = []
    for res in search_results or []:
        for a in res.get("albums") or []:
            out.append({
                "kind": "album",
                "uri": a.get("uri"),
                "title": a.get("name"),
                "artist": _artist_names(a),
            })
    return out


_CONTAINER_PREFIXES = ("tidal:album:", "tidal:playlist:", "tidal:mix:")


def _is_container_uri(uri: str) -> bool:
    """An album / playlist / mix URI — something that expands to many tracks."""
    return uri.startswith(_CONTAINER_PREFIXES)


def _container_noun(uri: str) -> str:
    if uri.startswith("tidal:playlist:"):
        return "playlist"
    if uri.startswith("tidal:mix:"):
        return "mix"
    return "album"


async def _expand_album(tc, album_uri: str) -> list[str]:
    """Resolve a tidal:album: URI to its track URIs via core.library.lookup."""
    res = await _rpc_resilient(tc, "core.library.lookup", {"uris": [album_uri]})
    tracks: list = []
    if isinstance(res, dict):
        for v in res.values():
            tracks += v or []
    elif isinstance(res, list):
        tracks = res
    return [t["uri"] for t in tracks if isinstance(t, dict) and t.get("uri")]


async def _expand_playable(tc, uri: str) -> list[str]:
    """Resolve any container URI (album / playlist / mix) to its track URIs. Albums go through
    core.library.lookup; playlists through core.playlists.get_items; mixes (and any other browsable
    node) through one level of core.library.browse, keeping only track refs."""
    if uri.startswith("tidal:album:"):
        return await _expand_album(tc, uri)
    if uri.startswith("tidal:playlist:"):
        items = await _rpc_resilient(tc, "core.playlists.get_items", {"uri": uri})
        return [t["uri"] for t in (items or []) if isinstance(t, dict) and t.get("uri")]
    refs = await _rpc_resilient(tc, "core.library.browse", {"uri": uri})
    return [r["uri"] for r in (refs or [])
            if isinstance(r, dict) and (r.get("uri") or "").startswith("tidal:track:")]


async def _play_uris(tc, uris: list[str]) -> None:
    await _rpc(tc, "core.tracklist.clear")
    await _rpc(tc, "core.tracklist.add", {"uris": uris})
    await _rpc(tc, "core.playback.play")


# -- backend callables -----------------------------------------------------

async def search(ctx, query="", limit=8) -> ToolResult:
    tc = ctx.config.tidal
    if not query:
        return ToolResult(output="", error="nothing to search for")
    try:
        results = await _rpc_resilient(tc, "core.library.search", {"query": {"any": [query]}})
    except Exception as e:
        return ToolResult(output="", error=f"TIDAL search failed: {_human_err(e)}")
    tracks = [t for t in _flatten_tracks(results) if t["uri"]][:limit]
    albums = [a for a in _flatten_albums(results) if a["uri"]][:4]
    return ToolResult(output=json.dumps(tracks + albums))


async def _hold_ambient(ctx) -> None:
    """Cap the just-started music at the ambient ceiling so VAD can hear the user over it. Best-effort
    (local import to avoid a cycle with the voice layer)."""
    try:
        from gabagent.voice.ducking import apply_ambient_cap
        await apply_ambient_cap(ctx)
    except Exception:
        pass


async def play(ctx, query="", uri="", album=False) -> ToolResult:
    tc = ctx.config.tidal
    # Container intent: a whole album/playlist/mix, not a single track. `album=true` with a query
    # searches for the album; a tidal:album/playlist/mix URI plays that exact container.
    if bool(album) or _is_container_uri(uri):
        return await _play_container(ctx, tc, query, uri, album=bool(album))
    label = ""
    if not uri and query:
        try:
            results = await _rpc_resilient(tc, "core.library.search", {"query": {"any": [query]}})
        except Exception as e:
            return ToolResult(output="", error=f"TIDAL search failed: {_human_err(e)}")
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
            return ToolResult(output="", error=f"couldn't resume: {_human_err(e)}")
    try:
        await _play_uris(tc, [uri])
    except Exception as e:
        return ToolResult(output="", error=f"couldn't play that: {_human_err(e)}")
    await _hold_ambient(ctx)
    return ToolResult(output=f"Playing {label} on TIDAL." if label else "Playing on TIDAL.")


async def _play_container(ctx, tc, query="", uri="", album=False) -> ToolResult:
    """Play a whole album / playlist / mix. A container URI plays exactly; `album=true` with a
    query searches the album catalog first."""
    label = ""
    container_uri = uri if _is_container_uri(uri) else ""
    if not container_uri and album and query:
        try:
            results = await _rpc_resilient(tc, "core.library.search", {"query": {"any": [query]}})
        except Exception as e:
            return ToolResult(output="", error=f"TIDAL search failed: {_human_err(e)}")
        albums = [a for a in _flatten_albums(results) if a["uri"]]
        if not albums:
            return ToolResult(output="", error=f"I couldn't find the album '{query}' on TIDAL.")
        container_uri = albums[0]["uri"]
        label = albums[0]["title"] + (f" by {albums[0]['artist']}" if albums[0]["artist"] else "")
    if not container_uri:
        return ToolResult(output="", error="no album, playlist, or mix to play")
    noun = _container_noun(container_uri)
    try:
        uris = await _expand_playable(tc, container_uri)
        if not uris:
            return ToolResult(output="", error=f"that {noun} has no playable tracks")
        await _play_uris(tc, uris)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't play that {noun}: {_human_err(e)}")
    await _hold_ambient(ctx)
    return ToolResult(output=f"Playing the {noun} {label} on TIDAL." if label
                      else f"Playing that {noun} on TIDAL.")


async def _transport(ctx, method: str, said: str) -> ToolResult:
    try:
        await _rpc(ctx.config.tidal, method)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't do that: {_human_err(e)}")
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
        return ToolResult(output="", error=f"couldn't check: {_human_err(e)}")
    if not track:
        return ToolResult(output=json.dumps({"playing": False}))
    artist = _artist_names(track)
    return ToolResult(output=json.dumps({
        "playing": True, "title": track.get("name"), "artist": artist,
        "album": (track.get("album") or {}).get("name", ""),
    }))


async def playlists(ctx) -> ToolResult:
    """The user's saved TIDAL playlists. Play one by passing its uri to tidal.play."""
    tc = ctx.config.tidal
    try:
        refs = await _rpc(tc, "core.playlists.as_list")
    except Exception as e:
        return ToolResult(output="", error=f"couldn't load your playlists: {_human_err(e)}")
    out = [{"kind": "playlist", "uri": p.get("uri"), "title": (p.get("name") or "").strip()}
           for p in (refs or []) if isinstance(p, dict) and p.get("uri")]
    return ToolResult(output=json.dumps(out))


# Personalized first (mixes built from listening history), then editorial fallbacks. mopidy-tidal's
# root browse ("tidal:") is empty on some versions, so target the known nodes directly and keep only
# directly-playable refs (mixes/playlists), not category directories.
_RECO_NODES = ("tidal:my_mixes", "tidal:for_you", "tidal:home")


async def recommendations(ctx) -> ToolResult:
    """Personalized mixes / recommendations. Play one by passing its uri to tidal.play."""
    tc = ctx.config.tidal
    for node in _RECO_NODES:
        try:
            refs = await _rpc(tc, "core.library.browse", {"uri": node})
        except Exception:
            continue
        items = [
            {"kind": _container_noun(r["uri"]), "uri": r["uri"], "title": (r.get("name") or "").strip()}
            for r in (refs or [])
            if isinstance(r, dict) and (r.get("uri") or "").startswith(("tidal:mix:", "tidal:playlist:"))
        ]
        if items:
            return ToolResult(output=json.dumps(items))
    return ToolResult(output="[]")
