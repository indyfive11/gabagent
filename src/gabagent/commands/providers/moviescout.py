"""MovieScout — voice "suggest N good movies to download" (first-party, GA-lane, read-only).

Taste-tuned recommender. The taste signal is OWNERSHIP itself, not the aggregate external ratings Radarr
carries (those are everyone's scores, never the user's personal taste — seeding by them converges on
blockbusters the user already knows). The flow: read the owned Radarr library live (one fast GET), sample
seeds proportional to the owned genre distribution, expand each seed via a PLUGGABLE discovery source (TMDB
today, Trakt-ready — see discovery_sources.py), then rank the candidates the user does NOT own by cross-seed
co-occurrence with a popularity penalty, so the output is adjacent-to-collection rather than famous canon.
Each suggestion carries its tmdb id / title / year — exactly radarr.add_movie's required slots — so
"add the first two" rides the existing tier-2 confirm with NO new add code.

detect() is config-only (radarr key + tmdb key present) — unset ⇒ the provider never surfaces, no HTTP,
host/satellite byte-identical (fail-soft). The only cache is the expensive discovery map (neighbor lists per owned
movie) in data_dir(), TTL'd and reused across asks; the owned metadata is already fast, so it isn't cached.
"""
from __future__ import annotations
import asyncio
import json
import random
import time
from collections import Counter
from dataclasses import asdict
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command, PyBackend, Slot
from gabagent.commands.providers import _arr
from gabagent.commands.providers.discovery_sources import Rec, select_source
from gabagent.config.paths import data_dir

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Output quality gate + popularity penalty thresholds. These are TMDB-UNIVERSAL (its global vote scale is
# the same for every install), NOT install-specific, so they are documented constants rather than config —
# per the generalization SOP's "a genuinely universal constant may stay hardcoded with a comment saying why".
_MIN_VOTE = 6.2          # drop candidates rated below this (kills junk; not a taste filter — see module doc)
_MIN_VOTES = 150         # …and with too few votes to trust the rating
_BLOCKBUSTER_VOTES = 8000  # at/above this vote_count a title is "famous canon" → demoted one rank tier so
                           # equally-adjacent-but-less-famous picks come first (adjacent-to-collection intent)
_SEED_CONCURRENCY = 6     # cap parallel discovery-source calls per ask (politeness + bounded wall-clock)

_RECS_PATH = data_dir() / "moviescout_recs.json"        # {"<source>:<tmdbId>": {"fetched": epoch, "recs":[…]}}
_OFFERED_PATH = data_dir() / "moviescout_offered.json"  # {"<tmdbId>": epoch_offered}


def _radarr_cfg(ctx):
    return getattr(getattr(ctx, "config", None), "radarr", None)


def _tmdb_cfg(ctx):
    return getattr(getattr(ctx, "config", None), "tmdb", None)


def _ms_cfg(ctx):
    return getattr(getattr(ctx, "config", None), "moviescout", None)


class MovieScoutProvider:
    id = "moviescout"

    async def detect(self, ctx: AgentContext) -> bool:
        # Pure config, no HTTP: needs a Radarr key (owned read + the add path) AND a discovery source key.
        # Trust each service's own reachability at call time — a 2nd probe here would just be redundant.
        r, t, m = _radarr_cfg(ctx), _tmdb_cfg(ctx), _ms_cfg(ctx)
        return bool(m and getattr(m, "enabled", True)
                    and r and getattr(r, "api_key", "")
                    and t and getattr(t, "api_key", ""))

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(
                id="moviescout.recommend", domain="media", tier=1, structured=True, featured=True,
                summary="Suggest good movies to download that AREN'T already in the library. Three ways: "
                        "with no filter it's surprise-me, tuned to what you own; pass `like` to anchor on a "
                        "specific film (even one not owned) — 'something like Blade Runner'; pass `genre` "
                        "and/or `decade` for a directed search — 'a good 90s sci-fi'. Returns candidates "
                        "with tmdb id, title and year — then use radarr.add_movie to add any the user picks. "
                        "Use this for 'suggest/recommend/find good movies', NOT radarr.search (one named title).",
                backend=PyBackend(ref="gabagent.commands.providers.moviescout:recommend"),
                params=[
                    Slot("count", "integer", False, description="how many to suggest (1-10, default 5)"),
                    Slot("like", "string", False, description="anchor on a specific movie by name, even one "
                         "not owned — 'something like Blade Runner' → like='Blade Runner'"),
                    Slot("genre", "string", False, description="filter by genre — 'sci-fi', 'horror', "
                         "'comedy', 'thriller'"),
                    Slot("decade", "integer", False, description="filter by decade start year — 1990 for "
                         "'90s movies', 2000 for 'the 2000s'"),
                ],
                examples=["suggest a few good movies to download", "recommend 3 movies I don't have",
                          "find me something like Interstellar", "suggest a good 90s sci-fi movie",
                          "recommend some good horror movies to download"],
            ),
        ]


PROVIDER = MovieScoutProvider()


# -- helpers ---------------------------------------------------------------------------------------

def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _rng(ctx) -> random.Random:
    # Tests set ctx.moviescout_rng_seed for deterministic sampling; production leaves it unset (None ⇒
    # system-seeded, so successive asks vary their seeds).
    return random.Random(getattr(ctx, "moviescout_rng_seed", None))


def _sample_seeds(owned: list[dict], k: int, rng: random.Random) -> list[dict]:
    """Weighted sample WITHOUT replacement, proportional to the owned genre distribution. A movie's weight
    is the summed share of its genres, so genres you own more of contribute proportionally more seeds
    (reflects real taste) while niche genres still appear — NOT even/stratified, which would dilute the
    dominant taste (K5)."""
    counts = Counter(g for m in owned for g in (m.get("genres") or []))
    total = sum(counts.values()) or 1
    pool = list(owned)
    weights = [max(sum(counts[g] for g in (m.get("genres") or [])) / total, 1e-9) for m in pool]
    picks: list[dict] = []
    for _ in range(min(k, len(pool))):
        total_w = sum(weights)
        if total_w <= 0:
            break
        r = rng.random() * total_w
        acc = 0.0
        for i, w in enumerate(weights):
            acc += w
            if acc >= r:
                picks.append(pool[i])
                weights[i] = 0.0   # drawn — exclude from the remaining draws
                break
    return picks


def _rank_key(rec: Rec, cooccur: int):
    """Sort key (used with reverse=True): most cross-seed co-occurrence first, then non-blockbusters before
    blockbusters, then higher rating. Co-occurrence is the primary taste signal (adjacent to MANY of your
    movies); the popularity penalty only reorders within a co-occurrence tier."""
    blockbuster = 1 if rec.votes >= _BLOCKBUSTER_VOTES else 0
    return (cooccur, -blockbuster, rec.vote)


def _load(path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _prune_recs(cache: dict, now: float, ttl: float) -> dict:
    """Drop expired neighbor-list entries on write, mirroring the offered store's prune — so the discovery
    cache can't grow unbounded (one entry per seed×page ever sampled) and re-parse cost stays bounded."""
    return {k: v for k, v in cache.items() if (now - v.get("fetched", 0)) < ttl}


def _decade_range(decade):
    """A decade start year (e.g. 1990) → (year_from, year_to) = (1990, 1999). Non-numeric/empty ⇒ (None,None)."""
    if decade in (None, "", 0):
        return None, None
    try:
        start = (int(decade) // 10) * 10
    except (TypeError, ValueError):
        return None, None
    return (start, start + 9) if start >= 1000 else (None, None)


def _directed_label(genre: str, year_from) -> str:
    """A human phrase for a directed filter, e.g. '90s science fiction' / 'science fiction' / '90s'."""
    parts = []
    if year_from:
        parts.append(f"{year_from % 100:02d}s")   # 1990 → "90s", 2000 → "00s"
    if genre:
        parts.append(genre)
    return " ".join(parts).strip()


def _because(mode: str, titles: list, anchor_title: str, genre: str, year_from) -> str:
    """The spoken 'why this pick' string, per mode."""
    if mode == "anchor":
        return f"like {anchor_title}" if anchor_title else ""
    if mode == "discover":
        label = _directed_label(genre, year_from)
        return f"a well-rated {label} pick" if label else "a well-rated pick"
    return ("you own " + " and ".join(titles)) if titles else ""


# -- backend ---------------------------------------------------------------------------------------

async def recommend(ctx, count=5, like="", genre="", decade=None) -> ToolResult:
    from gabagent.voice import events  # deferred (matches jellyfin.py) — avoids import cycles

    rcfg, tcfg, mcfg = _radarr_cfg(ctx), _tmdb_cfg(ctx), _ms_cfg(ctx)
    if not (rcfg and getattr(rcfg, "api_key", "")):
        return ToolResult(output="", error="Radarr isn't set up (settings: radarr.api_key).")
    source = select_source(tcfg)
    if source is None:
        return ToolResult(output="", error="Movie suggestions need a TMDB key (settings: tmdb.api_key).")

    n = _clamp(count, 1, 10, default=5)
    like = (like or "").strip()
    genre = (genre or "").strip()
    year_from, year_to = _decade_range(decade)
    # mode precedence: anchor on a named film ("like X") > directed filter (genre/decade) > surprise-me.
    mode = "anchor" if like else ("discover" if (genre or year_from) else "surprise")

    emit = getattr(ctx, "voice_emit", None)
    if emit:
        await emit(events.status("Looking now…"))

    # Owned library, live — the exclusion set (and, for surprise, the taste signal). NOT fatal for the
    # directed modes: you can still ask for "a good 90s sci-fi" with an empty library.
    try:
        async with _arr.client(rcfg) as c:
            r = await c.get("/api/v3/movie")
            owned = [m for m in (r.json() or []) if m.get("tmdbId")]
    except Exception as e:
        return ToolResult(output="", error=f"Couldn't read your movie library: {e}")
    owned_ids = {int(m["tmdbId"]) for m in owned}
    if mode == "surprise" and not owned:
        return ToolResult(output="", error="Your movie library is empty, so I have nothing to suggest from.")

    # shared recommender state
    recs_cache = _load(_RECS_PATH)
    ttl = max(1, getattr(mcfg, "recs_ttl_days", 45)) * 86400
    offered = _load(_OFFERED_PATH)
    cd = max(1, getattr(mcfg, "offered_cooldown_days", 21)) * 86400
    now = time.time()
    sem = asyncio.Semaphore(_SEED_CONCURRENCY)
    candidates: dict[int, dict] = {}
    dirty = False

    async def _fetch_page(seed, page):
        sid = int(seed["tmdbId"])
        key = f"{source.id}:{sid}:p{page}"
        ent = recs_cache.get(key)
        if ent and (now - ent.get("fetched", 0)) < ttl:
            return seed, [Rec(**x) for x in ent.get("recs", [])], None   # cache hit — nothing to persist
        async with sem:
            recs = await source.neighbors(sid, page)
        return seed, recs, key   # fresh — key signals "persist this"

    def _absorb(results):
        nonlocal dirty
        for res in results:
            if isinstance(res, BaseException):
                continue
            seed, recs, fresh_key = res
            if fresh_key:
                recs_cache[fresh_key] = {"fetched": now, "recs": [asdict(x) for x in recs]}
                dirty = True
            seed_title = seed.get("title") or ""
            for rec in recs:
                if rec.tmdb_id in owned_ids:
                    continue
                slot = candidates.setdefault(rec.tmdb_id, {"rec": rec, "cooccur": 0, "because": []})
                slot["cooccur"] += 1
                if seed_title and len(slot["because"]) < 2 and seed_title not in slot["because"]:
                    slot["because"].append(seed_title)

    def _ranked_picks():
        # output gate: drop junk (rating/vote-count floor), unreleased/dateless recs (year None → an ugly
        # "(None)" add-confirm and usually not yet grabbable), and titles offered within the cooldown.
        out = []
        for tid, slot in candidates.items():
            rec = slot["rec"]
            if rec.year is None or rec.vote < _MIN_VOTE or rec.votes < _MIN_VOTES:
                continue
            if mode == "surprise":                  # cooldown is a surprise-me NOVELTY concept; directed
                off = offered.get(str(tid))          # asks ("good 90s sci-fi", "like Interstellar") are
                if off and (now - off) < cd:         # repeatable/quasi-factual and should return the same
                    continue                         # canonical answers each time, not a worse second list.
            out.append(slot)
        if mode == "discover":
            # directed filter: rank by fame-within-the-filter (vote count), rating as tie-break — the
            # user wants the acclaimed KNOWN films in that lane, NOT the adjacent-obscure ones the
            # surprise-me/anchor popularity penalty prefers.
            out.sort(key=lambda s: (s["rec"].votes, s["rec"].vote), reverse=True)
        else:
            out.sort(key=lambda s: _rank_key(s["rec"], s["cooccur"]), reverse=True)
        return out

    # --- candidate generation by mode ---
    anchor_title = ""
    if mode == "discover":
        # directed filter — /discover queries, no seed/cache machinery (co-occurrence is degenerate here,
        # so every candidate gets cooccur=1 and ranks on fame-within-the-lane).
        def _absorb_discover(recs):
            for rec in recs:
                if rec.tmdb_id not in owned_ids:
                    candidates.setdefault(rec.tmdb_id, {"rec": rec, "cooccur": 1, "because": []})
        recs = await source.discover(genre=genre, year_from=year_from, year_to=year_to)
        if not recs:
            what = _directed_label(genre, year_from) or "movies"
            return ToolResult(output="", error=f"I couldn't find good {what} movies to suggest — "
                                               "try a different genre or decade.")
        _absorb_discover(recs)
        if len(_ranked_picks()) < n:   # thin → page 2 before the honest floor. Symmetric with the seed
            # path's rescue: a heavily-owned lane guts the top-20-by-votes (owned-exclusion), so a directed
            # ask on a large library is the MOST likely to thin — go one page deeper, never lower the gate.
            _absorb_discover(await source.discover(genre=genre, year_from=year_from,
                                                   year_to=year_to, page=2))
    else:
        # seed-based expansion. surprise = owned seeds sampled by genre share; anchor = one resolved named
        # film (even if unowned). Both share the neighbor machinery + the page-2 thin-yield rescue.
        if mode == "anchor":
            resolved = await source.resolve(like)
            if resolved is None:
                return ToolResult(output="", error=f"I couldn't find a movie called “{like}”.")
            anchor_title = resolved.title
            seeds = [{"tmdbId": resolved.tmdb_id, "title": resolved.title}]
        else:
            seeds = _sample_seeds(owned, max(1, getattr(mcfg, "seed_count", 12)), _rng(ctx))
        _absorb(await asyncio.gather(*[_fetch_page(s, 1) for s in seeds], return_exceptions=True))
        if len(_ranked_picks()) < n:   # thin → one page deeper for the same seeds before the honest floor
            _absorb(await asyncio.gather(*[_fetch_page(s, 2) for s in seeds], return_exceptions=True))
        if dirty:
            _save(_RECS_PATH, _prune_recs(recs_cache, now, ttl))

    top = _ranked_picks()[:n]
    if not top:
        return ToolResult(output="", error="I couldn't find good movies you don't already own right now — "
                                           "try again in a little while.")

    # record offered (surprise-me ONLY — see the cooldown note in _ranked_picks; directed modes neither
    # read nor write the store, so they stay repeatable and don't pollute surprise-me's novelty pool)
    if mode == "surprise":
        for s in top:
            offered[str(s["rec"].tmdb_id)] = now
        _save(_OFFERED_PATH, {k: v for k, v in offered.items() if (now - v) < cd})

    out = [{"tmdb_id": s["rec"].tmdb_id, "title": s["rec"].title, "year": s["rec"].year,
            "rating": round(s["rec"].vote, 1),
            "because": _because(mode, s["because"], anchor_title, genre, year_from)}
           for s in top]
    return ToolResult(output=json.dumps(out))
