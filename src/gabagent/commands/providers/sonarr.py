"""Sonarr provider — voice "add a show to download" + full control (first-party).

Sibling of the Radarr provider; shares the identical `/api/v3` + `X-Api-Key` plumbing via `_arr` (see its
zero-branch discipline). Series-specific bits live HERE (not in `_arr`): the add body echoes the looked-up
`seasons[]`, and `languageProfileId` is resolved at runtime — present + required on Sonarr v3, gone on v4 —
via `_resolve_language_profile_id` (include-when-resolvable; NEVER a config-hardcoded id).

Confirm safety mirrors Radarr: `add_show`/`delete` are the mutating commands, gated by TIER (tier-2 spoken /
tier-3 keyboard). `add_show` takes a RESOLVED tvdb_id + REQUIRED title + year so the tier-2 confirm always
speaks "{title} ({year})" (required slots only — a defaulted field would silently void the confirm at the
pre-exec gate). model-passed title/year are display-only; the POST body is rebuilt from an authoritative
re-lookup by tvdb_id.

Season semantics: for a series ALREADY in the library, an "add" is NOT a duplicate — it's a legitimate
"monitor another season" intent, so `add_show` reports it and points at `sonarr.monitor` rather than refusing.
"""
from __future__ import annotations
import json
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, PyBackend, Slot
from gabagent.commands.providers import _arr

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_APP = "Sonarr"


def _cfg(ctx):
    return getattr(getattr(ctx, "config", None), "sonarr", None)


async def _resolve_language_profile_id(c, configured):
    """Sonarr-only. (id | None). Include-when-resolvable, version-agnostic without parsing a version string:
    GET /api/v3/languageprofile — v3 returns profiles (the field is REQUIRED on add), v4 has removed language
    profiles (404 or empty list) though it still ACCEPTS+IGNORES the field on SeriesResource. So: return an id
    whenever the endpoint yields one (configured id if it still exists, else first); return None on 404/empty,
    and the caller omits the field entirely. Never raises."""
    try:
        r = await c.get("/api/v3/languageprofile")
        if r.status_code != 200:
            return None
        profiles = r.json() or []
    except Exception:
        return None
    if not profiles:
        return None
    if configured is not None:
        if any(p.get("id") == configured for p in profiles):
            return configured
    return profiles[0].get("id")


class SonarrProvider:
    id = "sonarr"

    async def detect(self, ctx: AgentContext) -> bool:
        return await _arr.detect(_cfg(ctx), _APP)

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(
                id="sonarr.search", domain="media", tier=1, structured=True, featured=True,
                summary="Search for a TV series to download via Sonarr — returns candidates with their tvdb "
                        "id, year, and whether it's already in your library. Use this first, then sonarr.add_show.",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:search"),
                params=[Slot("query", "string", True, description="the series title to look up")],
                examples=["find the show Severance to download", "look up Foundation on Sonarr",
                          "search Sonarr for The Bear"],
            ),
            Command(
                id="sonarr.add_show", domain="media", tier=2, featured=True, terminal_confirm=True,
                summary="Add a TV series to Sonarr so it downloads. Requires the tvdb id from sonarr.search; "
                        "pass the title and year so the spoken confirmation names the exact show.",
                confirm_template="Add {title} ({year}) to Sonarr?",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:add_show"),
                # title + year REQUIRED so the confirm_template always fills (defaulted slots are absent at the
                # pre-exec gate → would silently void the confirm). monitor scope stays OUT of the template.
                params=[
                    Slot("tvdb_id", "integer", True, description="the series' tvdb id, from sonarr.search"),
                    Slot("title", "string", True, description="the series title, for the spoken confirmation"),
                    Slot("year", "integer", True, description="the series' year, for the spoken confirmation"),
                ],
                examples=["add that show to download", "yes download Severance", "add the first one to Sonarr"],
            ),
            Command(
                id="sonarr.library", domain="media", tier=1, structured=True,
                summary="List the TV series already added to Sonarr (optionally only ones missing episodes).",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:library"),
                params=[
                    Slot("query", "string", False, description="filter by title substring"),
                    Slot("missing", "boolean", False, description="only series missing episodes"),
                ],
                examples=["what shows are in Sonarr", "what's missing episodes on Sonarr"],
            ),
            Command(
                id="sonarr.status", domain="media", tier=1, structured=True,
                summary="Check download progress in Sonarr — 'is the new episode downloaded yet?'. Reports the "
                        "queue with percent and time left.",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:status"),
                params=[Slot("query", "string", False, description="filter the queue by series title")],
                examples=["is the new Severance episode downloaded", "how's the Sonarr download going"],
            ),
            Command(
                id="sonarr.monitor", domain="media", tier=2,
                summary="Turn monitoring on or off for a series already in Sonarr, or for one of its seasons "
                        "(by the series' library id from sonarr.search/library).",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:monitor"),
                params=[
                    Slot("series_id", "integer", True, description="the Sonarr library id (not the tvdb id)"),
                    Slot("title", "string", False),
                    Slot("season", "integer", False,
                         description="optional — a single season number to (un)monitor; omit for the whole series"),
                    Slot("monitored", "boolean", False, description="true to monitor, false to stop (default true)"),
                ],
                examples=["monitor season 3 of Foundation on Sonarr", "stop monitoring that show"],
            ),
            Command(
                id="sonarr.research", domain="media", tier=2,
                summary="Kick off a fresh search for an already-added series in Sonarr (grab missing episodes now).",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:research"),
                params=[
                    Slot("series_id", "integer", True, description="the Sonarr library id"),
                    Slot("title", "string", False),
                ],
                examples=["search for that show again", "grab the missing episodes now on Sonarr"],
            ),
            Command(
                id="sonarr.delete", domain="media", tier=3,
                summary="Remove a series from Sonarr. Optionally also delete its downloaded files.",
                confirm_template="Remove {title} from Sonarr?",
                backend=PyBackend(ref="gabagent.commands.providers.sonarr:delete"),
                params=[
                    Slot("series_id", "integer", True, description="the Sonarr library id"),
                    Slot("title", "string", True, description="the series title, for the spoken confirmation"),
                    Slot("delete_files", "boolean", False, default=False,
                         description="true to also delete the downloaded files from disk (default false)"),
                ],
                examples=["remove Foundation from Sonarr", "delete that show and its files"],
            ),
        ]


PROVIDER = SonarrProvider()


# -- backend callables -----------------------------------------------------------------------------

def _no_key() -> ToolResult:
    return ToolResult(output="", error="Sonarr isn't set up (settings: sonarr.api_key).")


async def search(ctx, query="") -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not query:
        return ToolResult(output="", error="No series title to search for.")
    try:
        async with _arr.client(cfg) as c:
            items = await _arr.lookup(c, "/api/v3/series/lookup", query)
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr search failed: {e}")
    results = [
        {"tvdb_id": i.get("tvdbId"), "title": i.get("title"), "year": i.get("year"),
         "in_library": bool(i.get("id", 0)), "id": i.get("id", 0)}
        for i in items if i.get("tvdbId")
    ]
    return ToolResult(output=json.dumps(results))


async def add_show(ctx, tvdb_id=None, title="", year=None) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not tvdb_id:
        return ToolResult(output="", error="No tvdb id — search for the show first.")
    try:
        async with _arr.client(cfg) as c:
            # Authoritative re-lookup by tvdb id (Sonarr series/lookup supports the tvdb: prefix); match the id.
            items = await _arr.lookup(c, "/api/v3/series/lookup", f"tvdb:{int(tvdb_id)}")
            series = next((s for s in items if s.get("tvdbId") == int(tvdb_id)), None)
            if series is None:
                return ToolResult(output="", error="Sonarr couldn't find that show.")
            label = f"{series.get('title') or title or 'that show'}" + (
                f" ({series.get('year') or year})" if (series.get("year") or year) else "")
            if series.get("id", 0) > 0:
                # NOT a duplicate — an existing series + "add" is a monitor-a-new-season intent.
                return ToolResult(output=json.dumps({
                    "already_in_library": True, "series_id": series.get("id"), "title": series.get("title"),
                    "note": "Already in Sonarr — use sonarr.monitor with a season number to follow a new season."}))
            qp_id, err = await _arr.resolve_quality_profile(c, cfg.quality_profile)
            if err:
                return ToolResult(output="", error=f"Can't add {label}: {err}.")
            root, err = await _arr.resolve_root_folder(c, cfg.root_folder_path)
            if err:
                return ToolResult(output="", error=f"Can't add {label}: {err}.")
            body = dict(series)
            body.update({
                "qualityProfileId": qp_id,
                "rootFolderPath": root,
                "monitored": True,
                "seasonFolder": bool(cfg.season_folder),
                "addOptions": {"searchForMissingEpisodes": bool(cfg.search_on_add), "monitor": cfg.monitor},
            })
            lang_id = await _resolve_language_profile_id(c, None)
            if lang_id is not None:
                body["languageProfileId"] = lang_id
            pr = await c.post("/api/v3/series", json=body)
        if pr.status_code not in (200, 201):
            return ToolResult(output="", error=f"Sonarr rejected the add (HTTP {pr.status_code}).")
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr add failed: {e}")
    tail = " It'll start downloading." if cfg.search_on_add else " Monitoring is on."
    return ToolResult(output=f"Added {label} to Sonarr.{tail}")


async def library(ctx, query="", missing=False) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    try:
        async with _arr.client(cfg) as c:
            r = await c.get("/api/v3/series")
            shows = r.json() or []
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr library failed: {e}")
    q = (query or "").strip().lower()
    out = []
    for s in shows:
        if q and q not in (s.get("title") or "").lower():
            continue
        stats = s.get("statistics") or {}
        pct = stats.get("percentOfEpisodes")
        if missing and pct is not None and pct >= 100:
            continue
        out.append({"id": s.get("id"), "title": s.get("title"), "year": s.get("year"),
                    "percent_episodes": pct, "monitored": bool(s.get("monitored"))})
    return ToolResult(output=json.dumps(out))


async def status(ctx, query="") -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    try:
        async with _arr.client(cfg) as c:
            records = await _arr.queue_records(c)
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr status failed: {e}")
    q = (query or "").strip().lower()
    out = []
    for rec in records:
        title = rec.get("title") or (rec.get("series") or {}).get("title") or ""
        if q and q not in title.lower():
            continue
        size = rec.get("size") or 0
        left = rec.get("sizeleft")
        pct = round(100 * (1 - (left / size)), 1) if (size and left is not None) else None
        out.append({"title": title, "status": rec.get("status"), "percent": pct,
                    "time_left": rec.get("timeleft")})
    return ToolResult(output=json.dumps(out))


async def monitor(ctx, series_id=None, title="", season=None, monitored=True) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not series_id:
        return ToolResult(output="", error="Which show? I need its Sonarr library id (from search).")
    try:
        async with _arr.client(cfg) as c:
            r = await c.get(f"/api/v3/series/{int(series_id)}")
            if r.status_code != 200:
                return ToolResult(output="", error="That show isn't in Sonarr.")
            series = r.json() or {}
            if season is not None:
                found = False
                for sea in series.get("seasons") or []:
                    if sea.get("seasonNumber") == int(season):
                        sea["monitored"] = bool(monitored)
                        found = True
                if not found:
                    return ToolResult(output="", error=f"That show has no season {season}.")
            else:
                series["monitored"] = bool(monitored)
            pr = await c.put(f"/api/v3/series/{int(series_id)}", json=series)
        if pr.status_code not in (200, 202):
            return ToolResult(output="", error=f"Sonarr rejected the change (HTTP {pr.status_code}).")
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr monitor failed: {e}")
    label = title or series.get("title") or "that show"
    scope = f" season {season}" if season is not None else ""
    return ToolResult(output=f"{'Monitoring' if monitored else 'Stopped monitoring'}{scope} for {label} on Sonarr.")


async def research(ctx, series_id=None, title="") -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not series_id:
        return ToolResult(output="", error="Which show? I need its Sonarr library id.")
    try:
        async with _arr.client(cfg) as c:
            await _arr.command_post(c, "SeriesSearch", seriesId=int(series_id))
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr search failed: {e}")
    return ToolResult(output=f"Searching for {title or 'that show'} now on Sonarr.")


async def delete(ctx, series_id=None, title="", delete_files=False) -> ToolResult:
    cfg = _cfg(ctx)
    if not cfg or not cfg.api_key:
        return _no_key()
    if not series_id:
        return ToolResult(output="", error="Which show? I need its Sonarr library id.")
    try:
        async with _arr.client(cfg) as c:
            pr = await c.delete(f"/api/v3/series/{int(series_id)}",
                                params={"deleteFiles": "true" if delete_files else "false"})
        if pr.status_code not in (200, 202):
            return ToolResult(output="", error=f"Sonarr rejected the delete (HTTP {pr.status_code}).")
    except Exception as e:
        return ToolResult(output="", error=f"Sonarr delete failed: {e}")
    extra = " and deleted its files" if delete_files else ""
    return ToolResult(output=f"Removed {title or 'that show'} from Sonarr{extra}.")
