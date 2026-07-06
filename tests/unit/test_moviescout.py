"""MovieScout provider tests — respx-mocked Radarr owned-read + a STUBBED discovery source (never live
TMDB). Cache/offered files are redirected to tmp so tests never touch the real data_dir."""
import json
import types
import httpx
import respx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import moviescout as ms
from gabagent.commands.providers.discovery_sources import Rec

BASE = "http://radarr.test:7878"


# -- fixtures / helpers ----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_data(tmp_path, monkeypatch):
    """Redirect the two JSON stores into tmp so a test run never reads/writes the real data_dir."""
    monkeypatch.setattr(ms, "_RECS_PATH", tmp_path / "recs.json")
    monkeypatch.setattr(ms, "_OFFERED_PATH", tmp_path / "offered.json")


class FakeSource:
    """Deterministic stand-in for TmdbSource. `mapping` is {seed_tmdb_id: [Rec,…]} for page 1; `page2` is
    the optional {seed_tmdb_id: [Rec,…]} for page 2. `resolve_map` is {title: Rec|None} for anchor mode;
    `discover_result` is the list returned by discover() (directed mode)."""
    id = "tmdb"

    def __init__(self, mapping=None, page2=None, resolve_map=None, discover_result=None,
                 discover_page2=None):
        self.mapping = mapping or {}
        self.page2 = page2 or {}
        self.resolve_map = resolve_map or {}
        self.discover_result = discover_result or []
        self.discover_page2 = discover_page2 or []
        self.calls = []
        self.discover_calls = []

    def available(self):
        return True

    async def neighbors(self, tmdb_id, page=1):
        self.calls.append((tmdb_id, page))
        src = self.mapping if page == 1 else self.page2
        return list(src.get(tmdb_id, []))

    async def resolve(self, title):
        self.calls.append(("resolve", title))
        return self.resolve_map.get(title)

    async def discover(self, genre="", year_from=None, year_to=None, page=1):
        self.discover_calls.append((genre, year_from, year_to, page))
        return list(self.discover_page2 if page == 2 else self.discover_result)


def _ctx(**over):
    cfg = GabAgentConfig(api_key="test")
    cfg.radarr.base_url = BASE
    cfg.radarr.api_key = "k"
    cfg.tmdb.api_key = "t"
    for k, v in over.items():
        setattr(cfg.moviescout, k, v)
    # fixed rng seed ⇒ deterministic seed sampling; seed_count high ⇒ every owned movie becomes a seed,
    # so ranking assertions don't depend on which seeds were drawn.
    return types.SimpleNamespace(config=cfg, voice_emit=None, moviescout_rng_seed=7)


def _owned(*movies):
    """movies: (tmdb_id, title, [genres]) → the /api/v3/movie payload."""
    return respx.get(f"{BASE}/api/v3/movie").mock(return_value=httpx.Response(
        200, json=[{"tmdbId": t, "title": ti, "genres": g} for (t, ti, g) in movies]))


def _rec(tid, title="X", year=2020, vote=7.5, votes=1000, pop=10.0):
    return Rec(tmdb_id=tid, title=title, year=year, vote=vote, votes=votes, popularity=pop)


def _use_source(monkeypatch, mapping=None, page2=None, resolve_map=None, discover_result=None,
                discover_page2=None):
    src = FakeSource(mapping, page2, resolve_map, discover_result, discover_page2)
    monkeypatch.setattr(ms, "select_source", lambda cfg: src)
    return src


# -- detect (fail-soft config gate) ----------------------------------------------------------------

async def test_detect_needs_both_keys():
    assert await ms.PROVIDER.detect(_ctx()) is True
    c = _ctx(); c.config.tmdb.api_key = ""
    assert await ms.PROVIDER.detect(c) is False
    c = _ctx(); c.config.radarr.api_key = ""
    assert await ms.PROVIDER.detect(c) is False
    c = _ctx(enabled=False)
    assert await ms.PROVIDER.detect(c) is False


async def test_recommend_errors_without_tmdb_key(monkeypatch):
    # real select_source path (no monkeypatch) with an empty key ⇒ None ⇒ honest error, no HTTP.
    c = _ctx(); c.config.tmdb.api_key = ""
    res = await ms.recommend(c)
    assert res.error and "TMDB" in res.error


# -- happy path + addable shape --------------------------------------------------------------------

@respx.mock
async def test_recommend_returns_addable_unowned_picks(monkeypatch):
    _owned((1, "Owned A", ["Action"]), (2, "Owned B", ["Action"]))
    # seed 1 → recommends 100; seed 2 → recommends 100 (co-occurs) + 200
    src = _use_source(monkeypatch, {
        1: [_rec(100, "Great Adjacent", 2019, 7.8, 2000)],
        2: [_rec(100, "Great Adjacent", 2019, 7.8, 2000), _rec(200, "Also Good", 2021, 7.0, 800)],
    })
    res = await ms.recommend(_ctx(), count=5)
    out = json.loads(res.output)
    ids = [o["tmdb_id"] for o in out]
    assert 100 in ids and 200 in ids
    # owned excluded; each pick carries the add slots
    assert all(k in out[0] for k in ("tmdb_id", "title", "year"))
    # 100 co-occurs across both seeds → ranks first
    assert out[0]["tmdb_id"] == 100
    assert "Owned" in out[0]["because"]
    assert src.calls  # the source was consulted


@respx.mock
async def test_owned_titles_are_excluded_from_output(monkeypatch):
    _owned((1, "Owned A", ["Sci-Fi"]))
    _use_source(monkeypatch, {1: [_rec(1, "Owned A (dup)"), _rec(500, "New One")]})
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [500]


# -- ranking: co-occurrence primary, popularity penalty secondary ----------------------------------

@respx.mock
async def test_cooccurrence_outranks_single_seed(monkeypatch):
    _owned((1, "A", ["X"]), (2, "B", ["X"]), (3, "C", ["X"]))
    _use_source(monkeypatch, {
        1: [_rec(100, "Double")], 2: [_rec(100, "Double")],   # cooccur 2
        3: [_rec(200, "Single")],                              # cooccur 1
    })
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [100, 200]


@respx.mock
async def test_blockbuster_demoted_within_same_cooccurrence(monkeypatch):
    _owned((1, "A", ["X"]), (2, "B", ["X"]))
    _use_source(monkeypatch, {
        1: [_rec(100, "Famous", vote=8.5, votes=50000)],   # blockbuster
        2: [_rec(200, "Hidden Gem", vote=7.2, votes=400)],  # not
    })
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [200, 100]   # gem first despite lower rating


# -- output quality gate ---------------------------------------------------------------------------

@respx.mock
async def test_low_quality_candidates_dropped(monkeypatch):
    _owned((1, "A", ["X"]))
    _use_source(monkeypatch, {1: [
        _rec(100, "Too few votes", vote=8.0, votes=50),    # votes < 150
        _rec(200, "Too low rated", vote=5.0, votes=5000),  # vote < 6.2
        _rec(300, "Good", vote=6.5, votes=300),
    ]})
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [300]


# -- offered cooldown ------------------------------------------------------------------------------

@respx.mock
async def test_offered_within_cooldown_suppressed_and_recorded(monkeypatch):
    _owned((1, "A", ["X"]))
    _use_source(monkeypatch, {1: [_rec(100, "Recently offered"), _rec(200, "Fresh")]})
    import time
    ms._OFFERED_PATH.write_text(json.dumps({"100": time.time()}))   # 100 offered just now
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [200]
    # the newly-offered 200 is persisted
    assert "200" in json.loads(ms._OFFERED_PATH.read_text())


# -- cache: fresh fetch persisted, reused, source not re-hit ---------------------------------------

@respx.mock
async def test_recs_cache_persists_and_is_reused(monkeypatch):
    _owned((1, "A", ["X"]))
    src1 = _use_source(monkeypatch, {1: [_rec(100, "Cached")]})
    out1 = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out1] == [100]
    assert (1, 1) in src1.calls                    # fetched live (page 1)
    assert ms._RECS_PATH.exists()                  # persisted
    # second ask with a source that would return NOTHING — cache must serve it, source untouched
    src2 = _use_source(monkeypatch, {})
    # clear the offered store so 100 isn't cooldown-suppressed on the 2nd ask
    ms._OFFERED_PATH.write_text("{}")
    out2 = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out2] == [100]
    assert src2.calls == []                         # served from cache, not re-fetched


# -- count clamp + empty/none paths ----------------------------------------------------------------

@respx.mock
async def test_count_clamped(monkeypatch):
    _owned((1, "A", ["X"]))
    _use_source(monkeypatch, {1: [_rec(100 + i, f"M{i}", votes=1000) for i in range(20)]})
    out = json.loads((await ms.recommend(_ctx(), count=99)).output)
    assert len(out) == 10   # clamped to the max


@respx.mock
async def test_empty_library_errors(monkeypatch):
    _owned()   # no movies
    _use_source(monkeypatch, {})
    res = await ms.recommend(_ctx())
    assert res.error and "empty" in res.error.lower()


@respx.mock
async def test_no_candidates_errors(monkeypatch):
    _owned((1, "A", ["X"]))
    _use_source(monkeypatch, {1: []})   # source knows no neighbors
    res = await ms.recommend(_ctx())
    assert res.error and not res.output


# -- large-library thin-yield: page 2 rescues before the honest floor (VAC #2) ---------------------

@respx.mock
async def test_thin_page1_pulls_page2_before_honest_floor(monkeypatch):
    _owned((1, "A", ["X"]))
    # page 1 yields only ONE unowned candidate (< default n=5) → must go a page deeper
    src = _use_source(monkeypatch,
                      mapping={1: [_rec(100, "P1 pick")]},
                      page2={1: [_rec(200, "P2 pick"), _rec(300, "P2 pick 2")]})
    out = json.loads((await ms.recommend(_ctx())).output)
    ids = sorted(o["tmdb_id"] for o in out)
    assert ids == [100, 200, 300]                       # page-2 candidates rescued the thin list
    assert (1, 1) in src.calls and (1, 2) in src.calls  # both pages fetched


@respx.mock
async def test_page2_not_fetched_when_page1_is_enough(monkeypatch):
    _owned((1, "A", ["X"]))
    src = _use_source(monkeypatch,
                      mapping={1: [_rec(100 + i, f"M{i}", votes=1000) for i in range(6)]},  # 6 ≥ n=5
                      page2={1: [_rec(999, "unwanted")]})
    await ms.recommend(_ctx(), count=5)
    assert not any(p == 2 for (_, p) in src.calls)      # page 2 never touched — no wasted call


# -- VAC #4: dateless (year=None) recs are dropped from output -------------------------------------

@respx.mock
async def test_year_none_recs_dropped(monkeypatch):
    _owned((1, "A", ["X"]))
    _use_source(monkeypatch, {1: [
        _rec(100, "Unreleased", year=None, vote=8.0, votes=1000),
        _rec(200, "Released", year=2020, vote=7.0, votes=1000),
    ]})
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [200]


# -- VAC #5: recs cache prunes expired entries on write --------------------------------------------

@respx.mock
async def test_recs_cache_prunes_expired_on_write(monkeypatch):
    _owned((1, "A", ["X"]))
    import time
    # pre-seed the cache with a stale entry (fetched long ago) under an unrelated key + a fresh one
    stale = time.time() - 999 * 86400
    ms._RECS_PATH.write_text(json.dumps({
        "tmdb:9999:p1": {"fetched": stale, "recs": []},
    }))
    _use_source(monkeypatch, {1: [_rec(100, "Fresh")]})
    await ms.recommend(_ctx())
    saved = json.loads(ms._RECS_PATH.read_text())
    assert "tmdb:9999:p1" not in saved                  # stale entry pruned on write
    assert any(k.startswith("tmdb:1:") for k in saved)  # this ask's fresh entry kept


# -- directed search: anchor on a named film ("like X", even unowned) ------------------------------

@respx.mock
async def test_anchor_like_resolves_then_neighbors(monkeypatch):
    _owned((1, "Owned", ["X"]))   # Blade Runner is NOT owned
    anchor = _rec(78, "Blade Runner", 1982)
    src = _use_source(monkeypatch,
                      mapping={78: [_rec(100, "Neighbor A"), _rec(200, "Neighbor B")]},
                      resolve_map={"Blade Runner": anchor})
    out = json.loads((await ms.recommend(_ctx(), like="Blade Runner")).output)
    ids = sorted(o["tmdb_id"] for o in out)
    assert ids == [100, 200]                       # neighbors of the resolved film, not owned-seeded
    assert ("resolve", "Blade Runner") in src.calls
    assert (78, 1) in src.calls                     # neighbors() called on the RESOLVED id
    assert out[0]["because"] == "like Blade Runner"


@respx.mock
async def test_anchor_unknown_title_errors(monkeypatch):
    _owned((1, "Owned", ["X"]))
    _use_source(monkeypatch, resolve_map={})       # resolve returns None
    res = await ms.recommend(_ctx(), like="Not A Real Film")
    assert res.error and "Not A Real Film" in res.error and not res.output


@respx.mock
async def test_anchor_excludes_owned_neighbor(monkeypatch):
    _owned((1, "Owned", ["X"]), (100, "Also Owned", ["X"]))
    anchor = _rec(78, "Blade Runner", 1982)
    _use_source(monkeypatch,
                mapping={78: [_rec(100, "Owned neighbor"), _rec(200, "Fresh")]},
                resolve_map={"Blade Runner": anchor})
    out = json.loads((await ms.recommend(_ctx(), like="Blade Runner")).output)
    assert [o["tmdb_id"] for o in out] == [200]    # owned neighbor (100) dropped


# -- directed search: genre / decade filter via discover -------------------------------------------

@respx.mock
async def test_discover_by_genre_and_decade(monkeypatch):
    _owned((1, "Owned", ["X"]))
    src = _use_source(monkeypatch, discover_result=[
        _rec(300, "90s SciFi A", 1995, vote=7.8, votes=2000),
        _rec(301, "90s SciFi B", 1993, vote=7.1, votes=900),
    ])
    out = json.loads((await ms.recommend(_ctx(), count=2, genre="sci-fi", decade=1990)).output)
    assert [o["tmdb_id"] for o in out] == [300, 301]           # ranked by vote COUNT (fame) desc
    assert src.discover_calls == [("sci-fi", 1990, 1999, 1)]   # decade → (from,to) range; page 1 filled n=2
    assert "sci-fi" in out[0]["because"] and "90s" in out[0]["because"]
    assert not src.calls                                        # no neighbors/seed machinery in discover mode


@respx.mock
async def test_discover_ranks_by_fame_not_blockbuster_penalty(monkeypatch):
    # the anime-skew fix in miniature: a much-voted canonical film outranks a higher-RATED niche one
    # (opposite of the surprise-me penalty, which would demote the famous one).
    _owned((1, "Owned", ["X"]))
    _use_source(monkeypatch, discover_result=[
        _rec(400, "Niche high-avg", 1997, vote=8.3, votes=2000),    # higher rating, fewer votes
        _rec(401, "Canonical", 1999, vote=7.0, votes=25000),        # lower rating, hugely more votes
    ])
    out = json.loads((await ms.recommend(_ctx(), genre="sci-fi", decade=1990)).output)
    assert [o["tmdb_id"] for o in out] == [401, 400]              # famous-in-lane first


@respx.mock
async def test_discover_empty_result_errors(monkeypatch):
    _owned((1, "Owned", ["X"]))
    _use_source(monkeypatch, discover_result=[])   # unknown genre / nothing matches
    res = await ms.recommend(_ctx(), genre="nonsense-genre")
    assert res.error and not res.output


@respx.mock
async def test_discover_works_with_empty_library(monkeypatch):
    _owned()   # empty library is NOT fatal for a directed search
    _use_source(monkeypatch, discover_result=[_rec(300, "A Good One", 1995, votes=2000)])
    out = json.loads((await ms.recommend(_ctx(), genre="horror", decade=1990)).output)
    assert [o["tmdb_id"] for o in out] == [300]


@respx.mock
async def test_discover_page2_rescue_when_page1_thin(monkeypatch):
    # MED-1: a heavily-owned lane guts page 1 (owned-exclusion) → fall to page 2 before the honest floor.
    _owned((300, "Owned A", ["X"]), (301, "Owned B", ["X"]))   # both page-1 canonicals already owned
    src = _use_source(monkeypatch,
        discover_result=[_rec(300, "Owned A", 1995, votes=9000), _rec(301, "Owned B", 1996, votes=8000)],
        discover_page2=[_rec(302, "Unowned C", 1994, votes=2000), _rec(303, "Unowned D", 1997, votes=1500)])
    out = json.loads((await ms.recommend(_ctx(), genre="sci-fi", decade=1990)).output)
    assert [o["tmdb_id"] for o in out] == [302, 303]           # page-2 unowned filled the pool
    assert [c[3] for c in src.discover_calls] == [1, 2]        # page 1 then page 2 fetched


@respx.mock
async def test_discover_ignores_and_does_not_write_offered_cooldown(monkeypatch):
    # MED-2: directed asks are repeatable — a title offered "recently" must STILL come back, and a
    # directed result must NOT be recorded to the offered store (that's a surprise-me novelty concept).
    import time
    _owned((1, "Owned", ["X"]))
    _use_source(monkeypatch, discover_result=[_rec(300, "Canonical", 1995, votes=9000)])
    ms._OFFERED_PATH.write_text(json.dumps({"300": time.time()}))   # 300 "offered" moments ago
    out = json.loads((await ms.recommend(_ctx(), genre="sci-fi", decade=1990)).output)
    assert [o["tmdb_id"] for o in out] == [300]                     # NOT cooldown-suppressed for directed
    # and the directed result did not overwrite/expand the store beyond what was already there
    assert set(json.loads(ms._OFFERED_PATH.read_text())) == {"300"}


@respx.mock
async def test_no_directed_slots_is_surprise_mode(monkeypatch):
    # regression guard: with no like/genre/decade it must still be the owned-seeded surprise path
    _owned((1, "A", ["X"]), (2, "B", ["X"]))
    src = _use_source(monkeypatch, {1: [_rec(100, "P")], 2: [_rec(100, "P")]})
    out = json.loads((await ms.recommend(_ctx())).output)
    assert [o["tmdb_id"] for o in out] == [100]
    assert src.discover_calls == []                 # discover NOT used
    assert any(c[0] == 1 or c[0] == 2 for c in src.calls)   # owned seeds expanded


# -- genre-proportional seed sampling (determinism + proportionality) ------------------------------

def test_sample_seeds_is_genre_proportional_and_deterministic():
    import random
    owned = ([{"tmdbId": i, "title": f"act{i}", "genres": ["Action"]} for i in range(80)]
             + [{"tmdbId": 1000 + i, "title": f"doc{i}", "genres": ["Documentary"]} for i in range(20)])
    picks = ms._sample_seeds(owned, 40, random.Random(1))
    action = sum(1 for p in picks if "Action" in p["genres"])
    assert len(picks) == 40
    assert len(set(id(p) for p in picks)) == 40           # sampled WITHOUT replacement
    assert action > 40 * 0.6                              # 80/100 owned are Action ⇒ heavily favored
    # deterministic under a fixed rng seed
    again = ms._sample_seeds(owned, 40, random.Random(1))
    assert [p["tmdbId"] for p in picks] == [p["tmdbId"] for p in again]
