"""Service-agnostic plumbing shared by the Radarr and Sonarr providers.

Radarr and Sonarr are the same REST shape: base `/api/v3`, `X-Api-Key` header, identical
`system/status` / `qualityprofile` / `rootfolder` / `queue` / `command` endpoints. This module owns ONLY
that byte-identical plumbing so a fix lands once.

DISCIPLINE RULE (enforced by review): this module contains ZERO `if service == "radarr"/"sonarr"` branches.
The moment a helper needs to know which app called it, it does NOT belong here — push it back into the
caller (radarr.py / sonarr.py) and let it duplicate. Callers pass a config object and any app-specific
path/name as a parameter; they never pass a service tag.
"""
from __future__ import annotations
import httpx

_CALL_TIMEOUT = 15.0   # normal REST call
_DETECT_TIMEOUT = 2.0  # availability probe (runs under discovery's concurrent gate — must be short)


def client(cfg) -> httpx.AsyncClient:
    """An authed httpx client for a *arr instance (base_url + X-Api-Key). Caller uses `async with`."""
    return httpx.AsyncClient(
        base_url=cfg.base_url.rstrip("/"),
        headers={"X-Api-Key": cfg.api_key},
        timeout=_CALL_TIMEOUT,
    )


async def detect(cfg, app_name: str) -> bool:
    """Available iff enabled + api_key set + GET /api/v3/system/status 200 with the expected appName.
    Short-circuits BEFORE any HTTP when unconfigured, so an install with no *arr fires no pointless
    request. Unlike Jellyfin's keyless /System/Info/Public, *arr status 401s without the key — so this
    authed probe doubles as a key check. Never raises."""
    if not cfg or not getattr(cfg, "enabled", True) or not getattr(cfg, "api_key", ""):
        return False
    try:
        async with httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/"),
            headers={"X-Api-Key": cfg.api_key},
            timeout=_DETECT_TIMEOUT,
        ) as c:
            r = await c.get("/api/v3/system/status")
            return r.status_code == 200 and (r.json() or {}).get("appName") == app_name
    except Exception:
        return False


async def lookup(c: httpx.AsyncClient, path: str, term: str) -> list[dict]:
    """GET a lookup endpoint (movie/lookup or series/lookup — path is the caller's) → list of resources.
    A resource with id>0 is already in the library; id==0 (or absent) is a catalog match not yet added."""
    r = await c.get(path, params={"term": term})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.json() or []


async def resolve_root_folder(c: httpx.AsyncClient, configured_path: str) -> tuple[str | None, str | None]:
    """(path, error). Configured path wins if it exists+accessible; else auto-pick ONLY when exactly one
    accessible root exists; else fail loud (never silently route a large download to the wrong disk)."""
    r = await c.get("/api/v3/rootfolder")
    folders = [f for f in (r.json() or []) if f.get("accessible", True)]
    if not folders:
        return None, "there are no accessible download folders set up yet"
    if configured_path:
        want = configured_path.rstrip("/")
        for f in folders:
            if (f.get("path") or "").rstrip("/") == want:
                return f["path"], None
        return None, f"the configured download folder {configured_path} isn't set up"
    if len(folders) == 1:
        return folders[0]["path"], None
    return None, ("there are several download folders — set the root folder path in settings so I know "
                  "which disk to use")


async def resolve_quality_profile(c: httpx.AsyncClient, configured_name: str) -> tuple[int | None, str | None]:
    """(id, error). Configured NAME wins (portable across installs); else auto-pick ONLY when exactly one
    profile exists; else fail loud (the first profile is often "Any", which grabs any-quality releases)."""
    r = await c.get("/api/v3/qualityprofile")
    profiles = r.json() or []
    if not profiles:
        return None, "there are no quality profiles set up yet"
    if configured_name:
        want = configured_name.strip().lower()
        for p in profiles:
            if (p.get("name") or "").strip().lower() == want:
                return p["id"], None
        return None, f"the configured quality profile {configured_name!r} doesn't exist"
    if len(profiles) == 1:
        return profiles[0]["id"], None
    return None, ("there are several quality profiles — set the quality profile name in settings so I know "
                  "which to use")


async def queue_records(c: httpx.AsyncClient) -> list[dict]:
    """GET /api/v3/queue → the download records (the endpoint paginates as {records:[...]})."""
    r = await c.get("/api/v3/queue")
    data = r.json() or {}
    if isinstance(data, list):
        return data
    return data.get("records", []) or []


async def command_post(c: httpx.AsyncClient, name: str, **fields) -> dict:
    """POST /api/v3/command {name, **fields} — e.g. MoviesSearch/SeriesSearch grab-now."""
    r = await c.post("/api/v3/command", json={"name": name, **fields})
    return r.json() or {}
