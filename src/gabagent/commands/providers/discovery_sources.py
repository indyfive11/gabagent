"""Pluggable movie-discovery sources for MovieScout (named to avoid colliding with commands/discovery.py,
which builds the capability catalog — unrelated).

A discovery source answers three questions, each best-effort (never raises, so a dead call never sinks an
ask): `neighbors(tmdb_id)` — movies adjacent to one I own (the surprise-me + "like X" core); `resolve(title)`
— a free-text title → one movie (the "something like <film>" anchor, where the named film may not be owned);
`discover(genre, years)` — directed filter search ("a good 90s sci-fi"). TMDB is the first implementation; a
Trakt source later implements the same methods and slots in at `select_source` with zero caller change. The
seam stays minimal so adding a source grows no spine and duplicates no recommender policy (that lives in
MoviescoutConfig). A source that can't serve a mode returns []/None; the recommender degrades honestly.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
import httpx

_TMDB_BASE = "https://api.themoviedb.org/3"
_CALL_TIMEOUT = 4.0   # per-seed call ceiling — a cold ask fans these out in parallel under a semaphore
_DISCOVER_MIN_VOTES = 200  # vote_count floor sent to /discover so vote_average.desc doesn't surface single-
                           # vote 10.0 junk. TMDB-universal (its global vote scale), not install-specific.


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

    async def resolve(self, title: str) -> "Rec | None":
        """Resolve a free-text movie title to a single best-match Rec (for the 'something like <film>' anchor,
        where the named film need not be owned), or None if nothing matches. Best-effort."""
        ...

    async def discover(self, genre: str = "", year_from: int | None = None,
                       year_to: int | None = None, page: int = 1) -> list[Rec]:
        """Directed discovery by filter — a genre name and/or release-year range — rather than by a seed.
        Returns quality-sorted candidates ([] if unsupported, the genre is unknown, or on failure)."""
        ...


class TmdbSource:
    id = "tmdb"

    # Common voice forms → TMDB's canonical genre names (matched case-insensitively below).
    _GENRE_ALIASES = {"sci fi": "science fiction", "scifi": "science fiction", "sci-fi": "science fiction",
                      "rom com": "romance", "romcom": "romance", "docs": "documentary",
                      "kids": "family", "cartoon": "animation"}

    def __init__(self, api_key: str, lang: str = "en-US"):
        self._key = api_key or ""
        self._lang = lang or "en-US"
        self._genres: dict[str, int] | None = None   # lazily-fetched genre name→id map (per source instance)

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

    async def resolve(self, title: str) -> "Rec | None":
        if not title:
            return None
        try:
            async with httpx.AsyncClient(base_url=_TMDB_BASE, timeout=_CALL_TIMEOUT) as c:
                r = await c.get("/search/movie", params={"api_key": self._key, "language": self._lang,
                                                         "query": title, "page": 1})
            if r.status_code != 200:
                return None
            results = (r.json() or {}).get("results", [])
        except Exception:
            return None
        return _to_rec(results[0]) if (results and results[0].get("id")) else None

    async def discover(self, genre: str = "", year_from: int | None = None,
                       year_to: int | None = None, page: int = 1) -> list[Rec]:
        try:
            async with httpx.AsyncClient(base_url=_TMDB_BASE, timeout=_CALL_TIMEOUT) as c:
                # sort_by=vote_COUNT.desc (most-voted = most-watched/canonical within the filter), NOT
                # vote_average.desc — the latter surfaces high-average NICHE films (devoted-fanbase anime,
                # OVAs) over the acclaimed known ones a "good 90s sci-fi" ask wants. The vote_count floor
                # still drops single-vote junk; the recommender's rating gate (≥6.2) keeps quality. This is
                # the deliberate INVERSE of the surprise-me popularity penalty: directing by genre means the
                # user wants the famous-in-that-lane films, not adjacent-obscure ones.
                params = {"api_key": self._key, "language": self._lang, "page": max(1, int(page)),
                          "sort_by": "vote_count.desc", "vote_count.gte": _DISCOVER_MIN_VOTES}
                if genre:
                    gid = await self._genre_id(c, genre)
                    if gid is None:
                        return []                                 # unknown genre → caller gives honest error
                    params["with_genres"] = gid
                if year_from:
                    params["primary_release_date.gte"] = f"{int(year_from)}-01-01"
                if year_to:
                    params["primary_release_date.lte"] = f"{int(year_to)}-12-31"
                r = await c.get("/discover/movie", params=params)
            if r.status_code != 200:
                return []
            return [_to_rec(x) for x in (r.json() or {}).get("results", []) if x.get("id")]
        except Exception:
            return []

    async def _genre_id(self, c: httpx.AsyncClient, name: str) -> "int | None":
        want = self._GENRE_ALIASES.get(name.strip().lower(), name.strip().lower())
        if self._genres is None:
            try:
                r = await c.get("/genre/movie/list", params={"api_key": self._key, "language": self._lang})
                self._genres = ({(g.get("name") or "").lower(): g["id"] for g in (r.json() or {}).get("genres", [])}
                                if r.status_code == 200 else {})
            except Exception:
                self._genres = {}
        if want in self._genres:
            return self._genres[want]
        for gname, gid in self._genres.items():          # loose match ("sci-fi"→"science fiction", "crime"…)
            if want and (want in gname or gname in want):
                return gid
        return None


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
