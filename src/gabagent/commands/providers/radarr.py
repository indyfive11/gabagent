"""Radarr provider — voice "add a movie to download" + full control (first-party).

Mirrors the Jellyfin provider's shape (detect → commands → PyBackend callables) but shares its identical
`/api/v3` + `X-Api-Key` plumbing with Sonarr via `_arr` (see that module's zero-branch discipline rule).

Confirm safety: `add_movie`/`delete` are the mutating commands. The confirm gate is TIER (tier_of →
voice_approve): tier-2 = spoken yes/no, tier-3 = keyboard. `requires_confirm_surface` is dead code and is
deliberately NOT used here. `add_movie` takes a RESOLVED tmdb_id + title + year (from a prior `radarr.search`,
exactly as `jellyfin.play` takes a resolved item_id) so the tier-2 confirm always speaks the resolved
"{title} ({year})" — a wrong match is audible before the "yes", never a raw query.
"""
from __future__ import annotations
import json
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, PyBackend, Slot
from gabagent.commands.providers import _arr

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_APP = "Radarr"


def _cfg(ctx):
    return getattr(getattr(ctx, "config", None), "radarr", None)


class RadarrProvider:
    id = "radarr"

    async def detect(self, ctx: AgentContext) -> bool:
        return await _arr.detect(_cfg(ctx), _APP)

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(
                id="radarr.search", domain="media", tier=1, structured=True, featured=True,
                summary="Search for a movie to download via Radarr — returns candidates with their tmdb id, "
                        "year, and whether it's already in your library. Use this first, then radarr.add_movie.",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:search"),
                params=[Slot("query", "string", True, description="the movie title to look up")],
                examples=["find the movie Dune to download", "look up Foundation on Radarr",
                          "search Radarr for The Matrix"],
            ),
            Command(
                id="radarr.add_movie", domain="media", tier=2, featured=True, terminal_confirm=True,
                summary="Add a movie to Radarr so it downloads. Requires the tmdb id from radarr.search; "
                        "pass the title and year too so the spoken confirmation names the exact movie.",
                confirm_template="Add {title} ({year}) to Radarr?",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:add_movie"),
                # title + year are REQUIRED so the tier-2 confirm_template ALWAYS fills: _summarize_command
                # reads only the model-passed args at the pre-exec gate (slot DEFAULTS are applied later, in
                # the backend), so an optional field left to its default is absent there and would silently
                # void the whole confirm. They're display-only — the POST body is rebuilt from an
                # authoritative re-lookup by tmdb_id, never from these strings.
                params=[
                    Slot("tmdb_id", "integer", True, description="the movie's tmdb id, from radarr.search"),
                    Slot("title", "string", True, description="the movie title, for the spoken confirmation"),
                    Slot("year", "integer", True, description="the movie's year, for the spoken confirmation"),
                ],
                examples=["add that one to download", "yes download Dune", "add the first one to Radarr"],
            ),
            Command(
                id="radarr.library", domain="media", tier=1, structured=True,
                summary="List the movies already added to Radarr (optionally only monitored or missing ones).",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:library"),
                params=[
                    Slot("query", "string", False, description="filter by title substring"),
                    Slot("missing", "boolean", False, description="only movies with no downloaded file yet"),
                ],
                examples=["what movies are in Radarr", "what's still missing on Radarr"],
            ),
            Command(
                id="radarr.status", domain="media", tier=1, structured=True,
                summary="Check download progress in Radarr — 'is X downloaded yet?'. Reports the queue with "
                        "percent and time left.",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:status"),
                params=[Slot("query", "string", False, description="filter the queue by movie title")],
                examples=["is Dune downloaded yet", "how's the Radarr download going", "what's Radarr downloading"],
            ),
            Command(
                id="radarr.monitor", domain="media", tier=2,
                summary="Turn monitoring on or off for a movie already in Radarr (by its library id from "
                        "radarr.search/library).",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:monitor"),
                params=[
                    Slot("movie_id", "integer", True, description="the Radarr library id (not the tmdb id)"),
                    Slot("title", "string", False),
                    Slot("monitored", "boolean", False, description="true to monitor, false to stop (default true)"),
                ],
                examples=["stop monitoring that movie", "monitor Dune again on Radarr"],
            ),
            Command(
                id="radarr.research", domain="media", tier=2,
                summary="Kick off a fresh search for an already-added movie in Radarr (grab it now).",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:research"),
                params=[
                    Slot("movie_id", "integer", True, description="the Radarr library id"),
                    Slot("title", "string", False),
                ],
                examples=["search for that movie again", "try to grab Dune now on Radarr"],
            ),
            Command(
                id="radarr.delete", domain="media", tier=3,
                summary="Remove a movie from Radarr. Optionally also delete its downloaded files.",
                confirm_template="Remove {title} from Radarr?",
                backend=PyBackend(ref="gabagent.commands.providers.radarr:delete"),
                params=[
                    Slot("movie_id", "integer", True, description="the Radarr library id"),
                    Slot("title", "string", True, description="the movie title, for the spoken confirmation"),
                    Slot("delete_files", "boolean", False, default=False,
                         description="true to also delete the downloaded files from disk (default false)"),
                ],
                examples=["remove Dune from Radarr", "delete that movie and its files"],
            ),
        ]


PROVIDER = RadarrProvider()


# -- backend callables (async fn(ctx, **slots) -> ToolResult) --------------------------------------

def _no_key() -> ToolResult:
    return ToolResult(output="", error="Radarr isn't set up (settings: radarr.api_key).")


async def search(ctx, query="") -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not query:
        return ToolResult(output="", error="No movie title to search for.")
    try:
        async with _arr.client(cfg) as c:
            items = await _arr.lookup(c, "/api/v3/movie/lookup", query)
    except Exception as e:
        return ToolResult(output="", error=f"Radarr search failed: {e}")
    results = [
        {"tmdb_id": i.get("tmdbId"), "title": i.get("title"), "year": i.get("year"),
         "in_library": bool(i.get("id", 0)), "id": i.get("id", 0)}
        for i in items if i.get("tmdbId")
    ]
    return ToolResult(output=json.dumps(results))


async def add_movie(ctx, tmdb_id=None, title="", year=None) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not tmdb_id:
        return ToolResult(output="", error="No tmdb id — search for the movie first.")
    try:
        async with _arr.client(cfg) as c:
            r = await c.get("/api/v3/movie/lookup/tmdb", params={"tmdbId": int(tmdb_id)})
            if r.status_code != 200:
                return ToolResult(output="", error=f"Radarr couldn't look up that movie (HTTP {r.status_code}).")
            movie = r.json() or {}
            if not movie.get("tmdbId"):
                return ToolResult(output="", error="Radarr couldn't find that movie.")
            label = f"{movie.get('title') or title or 'that movie'}" + (
                f" ({movie.get('year') or year})" if (movie.get("year") or year) else "")
            if movie.get("id", 0) > 0:
                return ToolResult(output=f"{label} is already in your Radarr library.")
            qp_id, err = await _arr.resolve_quality_profile(c, cfg.quality_profile)
            if err:
                return ToolResult(output="", error=f"Can't add {label}: {err}.")
            root, err = await _arr.resolve_root_folder(c, cfg.root_folder_path)
            if err:
                return ToolResult(output="", error=f"Can't add {label}: {err}.")
            body = dict(movie)
            body.update({
                "qualityProfileId": qp_id,
                "rootFolderPath": root,
                "monitored": True,
                "minimumAvailability": cfg.minimum_availability,
                "addOptions": {"searchForMovie": bool(cfg.search_on_add), "monitor": cfg.monitor},
            })
            pr = await c.post("/api/v3/movie", json=body)
        if pr.status_code not in (200, 201):
            return ToolResult(output="", error=f"Radarr rejected the add (HTTP {pr.status_code}).")
    except Exception as e:
        return ToolResult(output="", error=f"Radarr add failed: {e}")
    tail = " It'll start downloading." if cfg.search_on_add else " Monitoring is on."
    return ToolResult(output=f"Added {label} to Radarr.{tail}")


async def library(ctx, query="", missing=False) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    try:
        async with _arr.client(cfg) as c:
            r = await c.get("/api/v3/movie")
            movies = r.json() or []
    except Exception as e:
        return ToolResult(output="", error=f"Radarr library failed: {e}")
    q = (query or "").strip().lower()
    out = []
    for m in movies:
        if q and q not in (m.get("title") or "").lower():
            continue
        if missing and m.get("hasFile"):
            continue
        out.append({"id": m.get("id"), "title": m.get("title"), "year": m.get("year"),
                    "has_file": bool(m.get("hasFile")), "monitored": bool(m.get("monitored"))})
    return ToolResult(output=json.dumps(out))


async def status(ctx, query="") -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    try:
        async with _arr.client(cfg) as c:
            records = await _arr.queue_records(c)
    except Exception as e:
        return ToolResult(output="", error=f"Radarr status failed: {e}")
    q = (query or "").strip().lower()
    out = []
    for rec in records:
        title = rec.get("title") or (rec.get("movie") or {}).get("title") or ""
        if q and q not in title.lower():
            continue
        size = rec.get("size") or 0
        left = rec.get("sizeleft")
        pct = round(100 * (1 - (left / size)), 1) if (size and left is not None) else None
        out.append({"title": title, "status": rec.get("status"), "percent": pct,
                    "time_left": rec.get("timeleft")})
    return ToolResult(output=json.dumps(out))


async def monitor(ctx, movie_id=None, title="", monitored=True) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not movie_id:
        return ToolResult(output="", error="Which movie? I need its Radarr library id (from search).")
    try:
        async with _arr.client(cfg) as c:
            r = await c.get(f"/api/v3/movie/{int(movie_id)}")
            if r.status_code != 200:
                return ToolResult(output="", error="That movie isn't in Radarr.")
            movie = r.json() or {}
            movie["monitored"] = bool(monitored)
            pr = await c.put(f"/api/v3/movie/{int(movie_id)}", json=movie)
        if pr.status_code not in (200, 202):
            return ToolResult(output="", error=f"Radarr rejected the change (HTTP {pr.status_code}).")
    except Exception as e:
        return ToolResult(output="", error=f"Radarr monitor failed: {e}")
    label = title or movie.get("title") or "that movie"
    return ToolResult(output=f"{'Monitoring' if monitored else 'Stopped monitoring'} {label} on Radarr.")


async def research(ctx, movie_id=None, title="") -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not movie_id:
        return ToolResult(output="", error="Which movie? I need its Radarr library id.")
    try:
        async with _arr.client(cfg) as c:
            await _arr.command_post(c, "MoviesSearch", movieIds=[int(movie_id)])
    except Exception as e:
        return ToolResult(output="", error=f"Radarr search failed: {e}")
    return ToolResult(output=f"Searching for {title or 'that movie'} now on Radarr.")


async def delete(ctx, movie_id=None, title="", delete_files=False) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not movie_id:
        return ToolResult(output="", error="Which movie? I need its Radarr library id.")
    try:
        async with _arr.client(cfg) as c:
            pr = await c.delete(f"/api/v3/movie/{int(movie_id)}",
                                params={"deleteFiles": "true" if delete_files else "false"})
        if pr.status_code not in (200, 202):
            return ToolResult(output="", error=f"Radarr rejected the delete (HTTP {pr.status_code}).")
    except Exception as e:
        return ToolResult(output="", error=f"Radarr delete failed: {e}")
    extra = " and deleted its files" if delete_files else ""
    return ToolResult(output=f"Removed {title or 'that movie'} from Radarr{extra}.")
