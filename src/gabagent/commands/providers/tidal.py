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
import contextlib
import difflib
import json
import re
import time
from typing import TYPE_CHECKING

import httpx

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, Slot, Detect, PyBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_RPC_TIMEOUT = 30.0   # search and mix/album expand hit TIDAL's live API, not the local cache
# The warm-cache retry (see _rpc_resilient) should return in ~0.5s once the first call has warmed TIDAL's
# cache. Bound it well under _RPC_TIMEOUT so a SECOND full timeout can't stack onto the first — a cold
# search that timed out at 30s then retried at 30s produced a 60s dead turn (live 2026-06-18, the worst
# single turn of the run). 8s is generous for an already-warmed call yet caps the worst case at ~38s.
_RETRY_TIMEOUT = 8.0

# Cached playback state so /media/state (→ inventory → sources()) can be served WITHOUT a live get_state
# RPC. Mopidy serializes JSON-RPC, so a get_state issued mid-search/play QUEUES behind that op and blocks
# the voice client's 2s /media/state poll for the whole op (~30s observed) — the gate goes blind to playback
# and reacts ~30s late. Instead: ops mark themselves in-flight (sources() then serves the cache, no RPC) and
# update the cache optimistically ('playing' at play-start so the gate reacts on time); sources() only hits
# Mopidy when idle AND the cache is stale. Lives on ctx so it's per-session and needs no global state.
_PLAYBACK_CACHE_TTL = 1.5   # seconds a cached state is served before an idle refresh RPC is allowed


def _pb_cache(ctx) -> dict:
    c = getattr(ctx, "_tidal_playback", None)
    if c is None:
        c = {"state": None, "ts": 0.0, "inflight": 0}
        ctx._tidal_playback = c
    return c


def _cache_playback(ctx, state) -> None:
    c = _pb_cache(ctx)
    c["state"] = state
    c["ts"] = time.monotonic()


@contextlib.asynccontextmanager
async def _tidal_op(ctx, *, optimistic=None, result=None):
    """Mark a TIDAL transport op in flight so sources() serves cached state (no get_state RPC that would
    queue behind this op in Mopidy and blind the gate). `optimistic` sets the cache at op start (e.g.
    'playing' so the gate reacts immediately, not after the search settles); `result` sets it on success."""
    c = _pb_cache(ctx)
    c["inflight"] += 1
    if optimistic is not None:
        _cache_playback(ctx, optimistic)
    try:
        yield
        if result is not None:
            _cache_playback(ctx, result)
    finally:
        c["inflight"] = max(0, c["inflight"] - 1)


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

    async def sources(self, ctx: AgentContext):
        """Mopidy/TIDAL is a single local player the brain owns — runs on this box, on the default sink.
        One owned-local audio source when something is loaded. Never raises."""
        tc = getattr(ctx.config, "tidal", None)
        if not tc or not getattr(tc, "enabled", False):
            return []
        c = _pb_cache(ctx)
        st = c["state"]
        # Only hit Mopidy when NO op is in flight AND the cache is stale — otherwise serve the cached state
        # so the poll never blocks behind an in-flight search/play (the ~30s gate-blind window). A live RPC
        # failure keeps the last cached state rather than dropping the source.
        if c["inflight"] == 0 and (time.monotonic() - c["ts"]) > _PLAYBACK_CACHE_TTL:
            try:
                st = await _rpc(tc, "core.playback.get_state", timeout=2.0)
                _cache_playback(ctx, st)
            except Exception:
                pass
        if st not in ("playing", "paused"):
            return []
        from gabagent.commands.media import MediaSource
        return [MediaSource(provider="tidal", kind="audio", state=st, owned=True, locality="local")]

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
                summary="Play music on TIDAL — a track, a whole album/playlist/mix, or resume. "
                        "For one of the user's OWN saved playlists by name ('play my Retro Favorites "
                        "playlist'), pass playlist='<the playlist name>' and Aria plays the WHOLE "
                        "playlist — do NOT pass the name as a plain query, which searches for a single "
                        "track and plays the wrong thing that stops after one song. "
                        "Pass shuffle=true to play a playlist/album/mix in random order "
                        "(use this for 'shuffle my playlist').",
                backend=PyBackend(ref=ref + "play"),
                params=[
                    Slot("query", "string", False, description="what to play, e.g. 'Kind of Blue'"),
                    Slot("uri", "string", False,
                         description="a tidal: URI from tidal.search/tidal.playlists to play exactly"),
                    Slot("album", "boolean", False,
                         description="true to queue the whole album instead of a single track"),
                    Slot("playlist", "string", False,
                         description="the user's OWN saved playlist NAME to play as a whole playlist, "
                                     "e.g. playlist='Retro Favorites' for 'play my Retro Favorites "
                                     "playlist'. Aria fuzzy-matches it against your saved playlists and "
                                     "plays the entire playlist, not a single track. A tidal: playlist "
                                     "URI is also accepted here."),
                    Slot("shuffle", "boolean", False,
                         description="true to play a playlist/album/mix in RANDOM order — set this "
                                     "whenever the request is to 'shuffle' a playlist or album"),
                ],
                examples=["play some Miles Davis on tidal", "play the album Dizzy Up the Girl",
                          "play kind of blue", "play music",
                          "play my Jaymes playlist (playlist='Jaymes')",
                          "shuffle my Retro Favorites playlist (playlist='Retro Favorites', shuffle=true)",
                          "play one of my mixes (uri from tidal.recommendations)"],
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
            Command(
                id="tidal.set_volume", domain="media", tier=1, featured=True, terminal_confirm=True,
                summary="Set the music volume to a specific level (0-100%) — use this for the MUSIC "
                        "volume, not the system volume",
                backend=PyBackend(ref=ref + "set_volume"),
                params=[Slot("level", "integer", True,
                             description="target volume as a percent, 0-100, e.g. 30")],
                examples=["set the music to 30%", "turn the music down to 20", "make the music quieter",
                          "music volume 50 percent", "lower the volume to 40"],
            ),
            Command(id="tidal.pause", domain="media", tier=1, featured=True, terminal_confirm=True,
                    summary="Pause TIDAL playback",
                    backend=PyBackend(ref=ref + "pause"), examples=["pause the music", "pause tidal"]),
            Command(id="tidal.resume", domain="media", tier=1, terminal_confirm=True, summary="Resume TIDAL playback",
                    backend=PyBackend(ref=ref + "resume"), examples=["resume", "keep playing"]),
            Command(id="tidal.next", domain="media", tier=1, featured=True, terminal_confirm=True,
                    summary="Skip to the next track",
                    backend=PyBackend(ref=ref + "next"), examples=["next song", "skip"]),
            Command(id="tidal.previous", domain="media", tier=1, terminal_confirm=True,
                    summary="Go to the previous track",
                    backend=PyBackend(ref=ref + "previous"), examples=["previous track", "go back a song"]),
            Command(id="tidal.stop", domain="media", tier=1, terminal_confirm=True, summary="Stop TIDAL playback",
                    backend=PyBackend(ref=ref + "stop"), examples=["stop the music"]),
            Command(id="tidal.now_playing", domain="media", tier=1, structured=True,
                    summary="What's playing on TIDAL right now",
                    backend=PyBackend(ref=ref + "now_playing"), examples=["what's playing", "what song is this"]),
            Command(
                id="tidal.shuffle", domain="media", tier=1, featured=True, terminal_confirm=True,
                summary="Shuffle the music that's ALREADY playing — reorders the current TIDAL queue, "
                        "turns on random play, and jumps to a fresh track; or turns shuffle off. (To "
                        "start a specific playlist shuffled, use tidal.play with shuffle=true instead.)",
                backend=PyBackend(ref=ref + "shuffle"),
                params=[Slot("mode", "string", False,
                             description="on, off, or toggle (default on)")],
                examples=["shuffle the music", "put it on shuffle", "randomize what's playing",
                          "mix it up", "turn off shuffle", "stop shuffling"],
            ),
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
    second succeeds (a direct browse then returns in ~0.5s). The retry uses a SHORT timeout
    (_RETRY_TIMEOUT): the cache is warm now, so it should return fast, and bounding it stops a second
    full 30s timeout from stacking into a ~60s dead turn."""
    try:
        return await _rpc(tc, method, params, timeout)
    except Exception as e:
        if not _is_timeout(e):
            raise
        return await _rpc(tc, method, params, min(timeout, _RETRY_TIMEOUT))


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


async def _playback_started(tc) -> bool:
    """True if Mopidy reports active playback. Used to reconcile a play RPC that *timed out* against
    reality: a slow `core.tracklist.add` (it resolves every track against TIDAL's live API) can blow the
    30s RPC timeout even after Mopidy has already started streaming track 1. Without this the caller
    raises and we tell the user 'it timed out' over music that is actually playing — and the LLM, seeing
    a failure, retries, doubling the dead time (live 2026-06-15: a random-playlist play hit back-to-back
    30s timeouts → a 99s turn, while media_state showed tidal:playing the whole time). Short timeout,
    never raises."""
    try:
        return (await _rpc(tc, "core.playback.get_state", timeout=2.0)) == "playing"
    except Exception:
        return False


async def _play_uris(tc, uris: list[str], shuffle: bool = False) -> dict:
    """Clear the tracklist, queue the URIs, and start playback. Returns per-step timings (ms) so the
    caller can localize where TIDAL play latency lands — `queue_ms` (tracklist.add, which resolves each
    track) vs `play_ms` (playback.play, which fetches the first stream URL from TIDAL's live API).
    `shuffle=True` randomizes the queued order BEFORE play, so playback starts on a random track and
    `core.tracklist.shuffle` (the in-place reorder) is the right primitive here — NOT set_random alone,
    which would leave track 1 first and only randomize what follows."""
    t = {}
    s = time.monotonic()
    await _rpc(tc, "core.tracklist.clear")
    t["clear_ms"] = int((time.monotonic() - s) * 1000)
    s = time.monotonic()
    await _rpc(tc, "core.tracklist.add", {"uris": uris})
    t["queue_ms"] = int((time.monotonic() - s) * 1000)
    if shuffle:
        await _rpc(tc, "core.tracklist.shuffle")                 # reorder the queue so play starts random
        await _rpc(tc, "core.tracklist.set_random", {"value": True})  # keep what follows random too
    s = time.monotonic()
    await _rpc(tc, "core.playback.play")
    t["play_ms"] = int((time.monotonic() - s) * 1000)
    return t


def _play_verb(shuffle: bool) -> str:
    return "Shuffling" if shuffle else "Playing"


def _play_timing(ctx, **fields) -> None:
    """Localize TIDAL play latency (search vs queue vs play-start vs ambient-hold) in voice_debug.jsonl —
    the 10–38s 'Trying tidal…' turns. Best-effort, gated by the voice debug path. Never raises."""
    try:
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "tidal_timing", **fields)
    except Exception:
        pass


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


async def _hold_ambient(ctx, new_track: bool = True) -> None:
    """Cap the just-started music at the ambient ceiling so VAD can hear the user over it. Best-effort
    (local import to avoid a cycle with the voice layer). `new_track` is False on a resume-in-place (the
    sink is unchanged) so apply_ambient_cap skips polling for a fresh sink-input."""
    try:
        from gabagent.voice.ducking import apply_ambient_cap, ensure_mopidy_sink_audible
        # Clear a stranded sink-input MUTE (a layer apply_ambient_cap's volume-mirror can't reach) so a
        # resumed/started track isn't silent at a healthy mixer level. Fire-and-forget: the Mopidy sink-input
        # doesn't exist until audio starts flowing (a few seconds after play), so ensure_mopidy_sink_audible
        # POLLS for it — running that inline would block the play path / delay the spoken reply. The mute-clear
        # is independent of the level-cap, so ordering doesn't matter.
        asyncio.create_task(ensure_mopidy_sink_audible(ctx))
        await apply_ambient_cap(ctx, new_track=new_track)
    except Exception:
        pass


async def play(ctx, query="", uri="", album=False, playlist="", shuffle=False) -> ToolResult:
    tc = ctx.config.tidal
    # Container intent: a whole album/playlist/mix, not a single track. A tidal:album/playlist/mix URI
    # plays that exact container; `album=true` with a query searches the album catalog.
    if _is_container_uri(uri):
        return await _play_container(ctx, tc, query, uri, album=bool(album), shuffle=bool(shuffle))
    # A saved playlist BY NAME. The model's dominant shape is tidal.play(playlist="<name>") — the name
    # carried in `playlist`, frequently with NO query. `playlist` used to be a boolean, so bool()-coercion
    # threw the name away (bool("Retro Favorites") -> True) and the call silently fell through to a bare
    # resume — the 2026-06-20 live "13 resumes, no music" bug. Now `playlist` carries the name itself; a
    # tidal: playlist URI handed there plays that exact playlist; the legacy query+playlist=true shape and
    # a stray bool-ish string still resolve (name lives in `query` then).
    if isinstance(playlist, str) and _is_container_uri(playlist.strip()):
        return await _play_container(ctx, tc, uri=playlist.strip(), shuffle=bool(shuffle))
    pl_name = _playlist_name_arg(playlist, query)
    if pl_name:
        return await _play_named_playlist(ctx, tc, pl_name, shuffle=bool(shuffle))
    if bool(album):
        return await _play_container(ctx, tc, query, uri, album=True, shuffle=bool(shuffle))
    # Safety net for a bare "play my <name>" where the model omitted playlist=true (live 2026-06-20:
    # "Play my James" played the track "James Joint", not the Jaymes playlist). If the query STRONGLY +
    # unambiguously matches a saved playlist, play the whole playlist (named, so a wrong guess is
    # audible) instead of a single track. A weak/ambiguous/no match falls through to the track search.
    if query and not uri:
        hit = await _maybe_saved_playlist(tc, query)
        if hit:
            return await _play_container(ctx, tc, uri=hit[0], shuffle=bool(shuffle), label=hit[1])
    # Mark the op in flight and optimistically cache "playing" so /media/state reflects it within ~100ms
    # (the gate reacts on time) instead of blocking ~30s behind the search/play (see _tidal_op). A failed
    # search/play self-heals: the cache reverts on the next idle poll once nothing's actually playing.
    async with _tidal_op(ctx, optimistic="playing"):
        t0 = time.monotonic()
        label = ""
        search_ms = None
        if not uri and query:
            _s = time.monotonic()
            try:
                results = await _rpc_resilient(tc, "core.library.search", {"query": {"any": [query]}})
            except Exception as e:
                return ToolResult(output="", error=f"TIDAL search failed: {_human_err(e)}")
            search_ms = int((time.monotonic() - _s) * 1000)
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
        # F2: if the resolved track is ALREADY the loaded/current one, resume in place instead of
        # clear+add+play — the latter restarts it from position 0 ("started over" instead of resuming).
        try:
            cur = await _rpc(tc, "core.playback.get_current_track", timeout=2.0)
        except Exception:
            cur = None
        if isinstance(cur, dict) and cur.get("uri") == uri:
            try:
                await _rpc(tc, "core.playback.resume", timeout=2.0)
            except Exception:
                pass
            await _hold_ambient(ctx, new_track=False)   # resume in place — same sink, no reconcile poll
            return ToolResult(output=f"Resuming {label} on TIDAL." if label else "Resuming on TIDAL.")
        try:
            timings = await _play_uris(tc, [uri], shuffle=bool(shuffle))
        except Exception as e:
            # Reconcile a timed-out play against real playback state — a slow track resolve can blow the
            # RPC timeout after streaming has already begun (see _playback_started).
            if _is_timeout(e) and await _playback_started(tc):
                await _hold_ambient(ctx)
                return ToolResult(output=f"Playing {label} on TIDAL." if label else "Playing on TIDAL.")
            return ToolResult(output="", error=f"couldn't play that: {_human_err(e)}")
        _h = time.monotonic()
        await _hold_ambient(ctx)
        _play_timing(ctx, phase="play", search_ms=search_ms, **timings,
                     hold_ms=int((time.monotonic() - _h) * 1000),
                     total_ms=int((time.monotonic() - t0) * 1000))
        return ToolResult(output=f"Playing {label} on TIDAL." if label else "Playing on TIDAL.")


# -- saved-playlist-by-name resolution (fuzzy match + confirm) -------------
# The model used to play "my Jaymes playlist" by passing the NAME as a track query → one wrong track
# that stops after it. Resolve the name to a playlist URI in CODE instead, with a confidence gate so a
# weak/ambiguous match asks "did you mean …?" rather than confidently playing the wrong playlist.
_PLAYLIST_PLAY_SCORE = 0.72   # explicit playlist=true: best match clears this to auto-play (else ask)
_PLAYLIST_STRONG = 0.82       # un-flagged "play my X": only a STRONG match silently beats a track
_PLAYLIST_ASK_FLOOR = 0.45    # below this the best match is too weak to even offer
_PLAYLIST_LEAD = 0.12         # best must beat the runner-up by this, else the two are "too close" → ask

_BOOLISH_TRUE = ("true", "1", "yes", "on")
_BOOLISH_FALSE = ("", "false", "0", "no", "off")


def _playlist_name_arg(playlist, query: str) -> str:
    """The saved-playlist NAME to play, normalized across the shapes the model actually sends.

    `playlist` may be the name itself — tidal.play(playlist="Retro Favorites"), the dominant shape —
    or a boolean / bool-ish string flag (the legacy shape, where the name lives in `query`), or empty.
    Returns "" when there's no play-a-named-playlist intent. A container URI handed in `playlist` is
    handled by the caller, so it resolves to "" here."""
    q = (query or "").strip()
    if playlist is True:
        return q
    if isinstance(playlist, str):
        pl = playlist.strip()
        low = pl.lower()
        if low in _BOOLISH_TRUE:
            return q                 # legacy: playlist=true is just a flag, the name is in `query`
        if low in _BOOLISH_FALSE or _is_container_uri(pl):
            return ""
        return pl                    # the name itself
    return ""


def _norm_title(s: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse — so 'Retro Favourites' ≈ 'Retro Favorites'."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _clean_playlist_query(q: str) -> str:
    """Reduce a spoken playlist request to just the NAME: drop a leading 'my/the/play' and a
    leading/trailing 'playlist' so 'play my Retro Favorites playlist' and 'my James' match cleanly."""
    toks = _norm_title(q).split()
    while toks and toks[0] in ("my", "the", "play", "playlist"):
        toks.pop(0)
    while toks and toks[-1] == "playlist":
        toks.pop()
    return " ".join(toks)


def _score_title(qn: str, tn: str) -> float:
    """Similarity of a normalized spoken name vs a normalized playlist title, 0..1."""
    if not qn or not tn:
        return 0.0
    score = difflib.SequenceMatcher(None, qn, tn).ratio()
    qt, tt = qn.split(), tn.split()
    if qt and set(qt) <= set(tt):                       # every spoken word appears in the title
        score = max(score, 0.9)
    elif len(qn) >= 4 and (qn in tn or tn in qn):       # clean substring (guard tiny tokens)
        score = max(score, 0.85)
    return score


def _resolve_playlist(playlists: list[dict], query: str, *,
                      play_score: float = _PLAYLIST_PLAY_SCORE, ask: bool = True):
    """Pick the saved playlist that best matches `query`. Returns one of:
    ("play", uri, title, [])         — confident, unambiguous best match (>= play_score, clear lead)
    ("ask", "", "", [titles])        — weak or near-tied; offer these to pick (only when ask=True)
    ("none", "", "", [])             — nothing close enough (or ambiguous with ask=False)."""
    qn = _clean_playlist_query(query)
    if not qn:
        return ("none", "", "", [])
    scored = sorted(
        ((_score_title(qn, _norm_title(p.get("title", ""))), p) for p in playlists if p.get("title")),
        key=lambda x: x[0], reverse=True)
    if not scored:
        return ("none", "", "", [])
    best_s, best = scored[0]
    second_s = scored[1][0] if len(scored) > 1 else 0.0
    if best_s >= play_score and (best_s - second_s) >= _PLAYLIST_LEAD:
        return ("play", best["uri"], best["title"], [])
    if ask and best_s >= _PLAYLIST_ASK_FLOOR:
        close = [p["title"] for s, p in scored
                 if s >= _PLAYLIST_ASK_FLOOR and (best_s - s) <= 0.15][:3]
        return ("ask", "", "", close or [best["title"]])
    return ("none", "", "", [])


async def _maybe_saved_playlist(tc, query: str):
    """Opportunistic, bounded: if `query` is a STRONG, unambiguous match to a saved playlist name,
    return (uri, title); else None. This is the safety net for a bare "play my <name>" where the model
    DIDN'T set playlist=true — a saved playlist beats a lone track that plays the wrong song and stops
    after it. Never stalls the track-play hot path (short timeout, no retry) and never raises."""
    try:
        refs = await _rpc(tc, "core.playlists.as_list", timeout=3.0)
    except Exception:
        return None
    pls = [{"uri": p.get("uri"), "title": (p.get("name") or "").strip()}
           for p in (refs or []) if isinstance(p, dict) and p.get("uri")]
    kind, uri, title, _ = _resolve_playlist(pls, query, play_score=_PLAYLIST_STRONG, ask=False)
    return (uri, title) if kind == "play" else None


def _english_list(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return "one of your playlists"
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} or {items[1]}"
    return ", ".join(items[:-1]) + f", or {items[-1]}"


async def _play_named_playlist(ctx, tc, query: str, shuffle: bool = False) -> ToolResult:
    """Resolve a saved-playlist NAME to its URI in code and play the whole playlist. Asks to
    disambiguate on a weak/ambiguous match, and NEVER falls back to a single track — a playlist
    request that can't be matched says so rather than confidently playing the wrong song."""
    try:
        pls = await _load_playlists(tc)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't load your playlists: {_human_err(e)}")
    kind, uri, title, options = _resolve_playlist(pls, query)
    if kind == "play":
        return await _play_container(ctx, tc, uri=uri, shuffle=bool(shuffle), label=title)
    if kind == "ask":
        return ToolResult(output=f"I have more than one playlist that could match — did you mean "
                                 f"{_english_list(options)}?")
    return ToolResult(output="", error=f"I couldn't find a saved playlist called '{query}'.")


async def _play_container(ctx, tc, query="", uri="", album=False, shuffle=False, label="") -> ToolResult:
    """Play a whole album / playlist / mix. A container URI plays exactly; `album=true` with a
    query searches the album catalog first. `shuffle=true` randomizes the queued order before play
    so it starts on a random track. `label` names the container in the spoken reply when the caller
    already resolved it (e.g. a playlist matched by name) — so a wrong pick is immediately audible."""
    # In-flight + optimistic "playing" so /media/state is served from cache during the search/expand/play
    # rather than blocking the gate's poll behind the op (see _tidal_op).
    async with _tidal_op(ctx, optimistic="playing"):
        t0 = time.monotonic()
        search_ms = None
        container_uri = uri if _is_container_uri(uri) else ""
        if not container_uri and album and query:
            _s = time.monotonic()
            try:
                results = await _rpc_resilient(tc, "core.library.search", {"query": {"any": [query]}})
            except Exception as e:
                return ToolResult(output="", error=f"TIDAL search failed: {_human_err(e)}")
            search_ms = int((time.monotonic() - _s) * 1000)
            albums = [a for a in _flatten_albums(results) if a["uri"]]
            if not albums:
                return ToolResult(output="", error=f"I couldn't find the album '{query}' on TIDAL.")
            container_uri = albums[0]["uri"]
            label = albums[0]["title"] + (f" by {albums[0]['artist']}" if albums[0]["artist"] else "")
        if not container_uri:
            return ToolResult(output="", error="no album, playlist, or mix to play")
        noun = _container_noun(container_uri)
        try:
            _e = time.monotonic()
            uris = await _expand_playable(tc, container_uri)
            expand_ms = int((time.monotonic() - _e) * 1000)
            if not uris:
                return ToolResult(output="", error=f"that {noun} has no playable tracks")
            timings = await _play_uris(tc, uris, shuffle=bool(shuffle))
        except Exception as e:
            # A slow live-API call can blow the RPC timeout after Mopidy already started streaming —
            # reconcile against real playback state before calling it a failure (see _playback_started).
            if _is_timeout(e) and await _playback_started(tc):
                await _hold_ambient(ctx)
                return ToolResult(output=f"{_play_verb(shuffle)} the {noun} {label} on TIDAL." if label
                                  else f"{_play_verb(shuffle)} that {noun} on TIDAL.")
            return ToolResult(output="", error=f"couldn't play that {noun}: {_human_err(e)}")
        _h = time.monotonic()
        await _hold_ambient(ctx)
        _play_timing(ctx, phase="play_container", search_ms=search_ms, expand_ms=expand_ms, **timings,
                     hold_ms=int((time.monotonic() - _h) * 1000),
                     total_ms=int((time.monotonic() - t0) * 1000))
        return ToolResult(output=f"{_play_verb(shuffle)} the {noun} {label} on TIDAL." if label
                          else f"{_play_verb(shuffle)} that {noun} on TIDAL.")


_TRANSPORT_RESULT = {
    "core.playback.pause": "paused", "core.playback.resume": "playing",
    "core.playback.next": "playing", "core.playback.previous": "playing",
    "core.playback.stop": "stopped",
}


async def _transport(ctx, method: str, said: str) -> ToolResult:
    try:
        async with _tidal_op(ctx, result=_TRANSPORT_RESULT.get(method)):
            await _rpc(ctx.config.tidal, method)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't do that: {_human_err(e)}")
    return ToolResult(output=said)


def _volume_floor(ctx) -> int:
    """Clamp an absolute volume set up to at least this % so 'turn it way down' can't ratchet the
    music to inaudible (to truly silence it the user pauses/stops). Config media_volume_floor."""
    try:
        f = int(getattr(ctx.config, "media_volume_floor", 5))
    except Exception:
        f = 5
    return max(0, min(100, f))


async def _set_stream_volume(ctx, target: int) -> None:
    """Set the MUSIC stream's volume — the Mopidy software mixer AND its PipeWire sink-input — never the
    system master sink (@DEFAULT_SINK@). Targeting the master sink (as system.volume_* does) would also
    attenuate the assistant's own voice, which shares that sink. Best-effort on the sink-input."""
    await _rpc(ctx.config.tidal, "core.mixer.set_volume", {"volume": target}, timeout=2.0)
    try:
        # Local import to avoid a module-load cycle (ducking imports this module's _rpc), mirroring
        # _hold_ambient. The sink-input is the node the user actually hears.
        from gabagent.voice.ducking import _mopidy_sink_input, _run_pactl
        found = await _mopidy_sink_input()
        if found:
            await _run_pactl("set-sink-input-volume", found[0], f"{target}%")
    except Exception:
        pass


async def set_volume(ctx, level=None) -> ToolResult:
    """Set the music volume to an absolute level (0-100), targeting the music stream — idempotent, with
    a floor so it can't be ratcheted to silence. Replaces brute-forced relative system volume steps."""
    if level is None or level == "":
        return ToolResult(output="", error="no volume level given")
    try:
        target = int(round(float(level)))
    except (TypeError, ValueError):
        return ToolResult(output="", error="that volume isn't a number")
    target = max(_volume_floor(ctx), min(100, target))
    try:
        # If a duck/command window is open, don't write the live output — that would unduck the bed
        # mid-window and re-open the music-leak the duck suppresses. Instead record the new level so the
        # duck-off restore returns to it (option b). With no duck active, set the live stream as usual.
        from gabagent.voice.ducking import note_user_volume
        if not note_user_volume(ctx, target):
            await _set_stream_volume(ctx, target)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't set the volume: {_human_err(e)}")
    return ToolResult(output=f"Set the music volume to {target} percent.")


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
    info = {
        "playing": True, "title": track.get("name"), "artist": artist,
        "album": (track.get("album") or {}).get("name", ""),
    }
    # A loaded track is NOT proof the user can hear it: the music sink-input can be muted, at 0%, or routed
    # to another device while the player still reports "playing". Probe the real output so the brain answers
    # "is it playing?" honestly instead of asserting playback into silence (the "I verified it's playing —
    # check your sound settings" over-claim, which sends the user chasing their own audio). Best-effort: if
    # the probe can't tell, leave the field off so grounding stays neutral.
    try:
        from gabagent.voice.ducking import mopidy_audibility
        aud = await mopidy_audibility()
        if aud.get("checked"):
            info["audible"] = bool(aud.get("audible"))
            if not aud.get("audible") and aud.get("reason"):
                info["not_audible_reason"] = aud["reason"]
        # Capture the verdict in voice_debug.jsonl: the verify path's audibility (mute/volume/routing) is
        # otherwise invisible in the brain log — this is what proves the honesty fix fired AND captures the
        # mute-vs-wrong-sink-routing distinction for the silence root-cause (VAC's diagnostic ask).
        try:
            from gabagent.voice.debuglog import dlog
            dlog(ctx, "audibility", **{k: aud[k] for k in
                 ("checked", "present", "audible", "muted", "volume", "sink",
                  "default_sink", "on_default", "reason") if k in aud})
        except Exception:
            pass
    except Exception:
        pass
    return ToolResult(output=json.dumps(info))


_SHUFFLE_OFF_WORDS = {"off", "false", "no", "stop", "disable", "unshuffle", "0"}
_SHUFFLE_TOGGLE_WORDS = {"toggle", "flip", "switch"}


async def shuffle(ctx, mode="on") -> ToolResult:
    """Shuffle the TIDAL/Mopidy music that's already playing. `mode` is on | off | toggle (default on).

    ON actually reorders the current queue (`core.tracklist.shuffle` — the in-place reorder), turns on
    random play for what follows, and, if music is playing, skips to a freshly-shuffled track so the
    change is immediately audible. `set_random` ALONE is the wrong primitive — it leaves the current
    track playing and only randomizes the NEXT selection, so 'shuffle it' would keep playing the same
    song (the 2026-06-19 live bug). OFF turns random play off and leaves the queue order as-is.

    (To START a specific playlist/album shuffled from nothing, use tidal.play with shuffle=true.)"""
    tc = ctx.config.tidal
    m = (mode or "on").strip().lower()
    try:
        if m in _SHUFFLE_TOGGLE_WORDS:
            target = not bool(await _rpc(tc, "core.tracklist.get_random", timeout=2.0))
        elif m in _SHUFFLE_OFF_WORDS:
            target = False
        else:  # on / true / yes / shuffle / randomize / random / anything else → the natural "shuffle it"
            target = True
        if target:
            await _rpc(tc, "core.tracklist.shuffle", timeout=2.0)          # reorder the queue in place
            await _rpc(tc, "core.tracklist.set_random", {"value": True}, timeout=2.0)
            try:  # jump to a fresh track so the reshuffle is audible NOW (Rob: "jump to a new track")
                if (await _rpc(tc, "core.playback.get_state", timeout=2.0)) == "playing":
                    await _rpc(tc, "core.playback.next", timeout=2.0)
            except Exception:
                pass
        else:
            await _rpc(tc, "core.tracklist.set_random", {"value": False}, timeout=2.0)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't change shuffle: {_human_err(e)}")
    return ToolResult(output="Shuffle on." if target else "Shuffle off.")


async def _load_playlists(tc) -> list[dict]:
    """The user's saved TIDAL playlists as [{uri, title}]. An EMPTY first result is the cold-cache
    false-negative signature: mopidy-tidal returns [] before its live playlist set is warmed, and the
    brain then confidently tells the user a playlist they DO own "doesn't exist" (live 2026-06-17:
    "Retro Favorites" reported absent 3×, played fine the next day). A genuinely empty library just
    re-returns []; one warm-up retry rules out the cold miss cheaply. Raises on a transport error."""
    refs = await _rpc_resilient(tc, "core.playlists.as_list")
    if not refs:
        refs = await _rpc_resilient(tc, "core.playlists.as_list")
    return [{"uri": p.get("uri"), "title": (p.get("name") or "").strip()}
            for p in (refs or []) if isinstance(p, dict) and p.get("uri")]


async def playlists(ctx) -> ToolResult:
    """The user's saved TIDAL playlists. Play one by passing its uri to tidal.play."""
    tc = ctx.config.tidal
    try:
        pls = await _load_playlists(tc)
    except Exception as e:
        return ToolResult(output="", error=f"couldn't load your playlists: {_human_err(e)}")
    out = [{"kind": "playlist", "uri": p["uri"], "title": p["title"]} for p in pls]
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
