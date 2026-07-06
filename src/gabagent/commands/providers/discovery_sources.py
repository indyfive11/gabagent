"""Pluggable movie-discovery sources for MovieScout (named to avoid colliding with commands/discovery.py,
which builds the capability catalog — unrelated).

A discovery source answers ONE question: "given a movie I own (by its TMDB id), what movies are adjacent
to it?" TMDB is the first implementation; a Trakt source later implements the same single `neighbors`
method and slots in at `select_source` with zero caller change. The seam is deliberately minimal so adding
a source grows no spine and duplicates no recommender policy (that lives in MoviescoutConfig).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
import httpx

_TMDB_BASE = "https://api.themoviedb.org/3"
_CALL_TIMEOUT = 4.0   # per-seed call ceiling — a cold ask fans these out in parallel under a semaphore


@dataclass(frozen=True)
class Rec:
    """A candidate movie adjacent to a seed. Source-neutral shape; the recommender ranks on these fields."""
    tmdb_id: int
    title: str
    year: int | None
    vote: float        # average rating (TMDB vote_average, 0-10)
    votes: int         # vote_count — doubles as the popularity/quality signal for the output gate + penalty
    popularity: float  # TMDB popularity score (unused in v1 ranking; carried for future tuning)


@runtime_checkable
class DiscoverySource(Protocol):
    id: str
    def available(self) -> bool:
        """True iff this source has what it needs to run (e.g. an API key). Pure config — no I/O."""
        ...

    async def neighbors(self, tmdb_id: int, page: int = 1) -> list[Rec]:
        """Movies adjacent to the given TMDB movie id, one result page (`page`, 1-based ~20 items).
        Best-effort: returns [] on any failure, never raises, so one dead seed never sinks the whole ask.
        A source that can't page may ignore `page` and return [] for page>1."""
        ...


class TmdbSource:
    id = "tmdb"

    def __init__(self, api_key: str, lang: str = "en-US"):
        self._key = api_key or ""
        self._lang = lang or "en-US"

    def available(self) -> bool:
        return bool(self._key)

    async def neighbors(self, tmdb_id: int, page: int = 1) -> list[Rec]:
        # /recommendations is COLLABORATIVE ("people who liked X also liked…") — the taste-adjacent signal.
        # /similar is genre-generic; use it ONLY as a fallback when recommendations is empty (obscure/new
        # seed with no collaborative data), never in preference to it. One client covers both fetches.
        try:
            async with httpx.AsyncClient(base_url=_TMDB_BASE, timeout=_CALL_TIMEOUT) as c:
                for path in ("recommendations", "similar"):
                    recs = await self._fetch(c, tmdb_id, path, page)
                    if recs:
                        return recs
        except Exception:
            return []
        return []

    async def _fetch(self, c: httpx.AsyncClient, tmdb_id: int, path: str, page: int = 1) -> list[Rec]:
        try:
            r = await c.get(f"/movie/{int(tmdb_id)}/{path}",
                            params={"api_key": self._key, "language": self._lang, "page": max(1, int(page))})
            if r.status_code != 200:
                return []
            return [_to_rec(x) for x in (r.json() or {}).get("results", []) if x.get("id")]
        except Exception:
            return []


def _to_rec(x: dict) -> Rec:
    rd = (x.get("release_date") or "")[:4]
    return Rec(
        tmdb_id=int(x["id"]),
        title=x.get("title") or x.get("name") or "",
        year=int(rd) if rd.isdigit() else None,
        vote=float(x.get("vote_average") or 0.0),
        votes=int(x.get("vote_count") or 0),
        popularity=float(x.get("popularity") or 0.0),
    )


def select_source(tmdb_cfg) -> DiscoverySource | None:
    """The active discovery source for this install. TMDB today; a Trakt source slots in here (first
    available wins) with zero change to the MovieScout provider. None ⇒ no source configured ⇒ MovieScout
    stays hidden (its detect() also gates on the same key, so this is a belt-and-suspenders guard)."""
    src = TmdbSource(getattr(tmdb_cfg, "api_key", "") or "", getattr(tmdb_cfg, "lang", "en-US") or "en-US")
    return src if src.available() else None
