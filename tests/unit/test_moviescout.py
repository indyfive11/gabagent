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
    the optional {seed_tmdb_id: [Rec,…]} served for page 2 (empty ⇒ page 2 returns nothing)."""
    id = "tmdb"

    def __init__(self, mapping, page2=None):
        self.mapping = mapping
        self.page2 = page2 or {}
        self.calls = []

    def available(self):
        return True

    async def neighbors(self, tmdb_id, page=1):
        self.calls.append((tmdb_id, page))
        src = self.mapping if page == 1 else self.page2
        return list(src.get(tmdb_id, []))


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


def _use_source(monkeypatch, mapping, page2=None):
    src = FakeSource(mapping, page2)
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
