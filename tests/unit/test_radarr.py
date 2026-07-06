"""Radarr provider tests — respx-mocked against a FAKE host + FAKE key (never a live instance)."""
import json
import types
import httpx
import respx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import radarr as rd

BASE = "http://radarr.test:7878"


def _ctx(**over):
    cfg = GabAgentConfig(api_key="test")
    cfg.radarr.base_url = BASE
    cfg.radarr.api_key = "k"
    for k, v in over.items():
        setattr(cfg.radarr, k, v)
    return types.SimpleNamespace(config=cfg)


def _status(name="Radarr"):
    return respx.get(f"{BASE}/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"appName": name, "version": "5.0"}))


# -- detect ----------------------------------------------------------------------------------------

@respx.mock
async def test_detect_true_when_reachable_and_named():
    _status()
    assert await rd.PROVIDER.detect(_ctx()) is True


@respx.mock
async def test_detect_false_on_wrong_appname():
    _status("Sonarr")   # a Sonarr answering on the Radarr port must NOT pass
    assert await rd.PROVIDER.detect(_ctx()) is False


@respx.mock
async def test_detect_false_on_401():
    respx.get(f"{BASE}/api/v3/system/status").mock(return_value=httpx.Response(401))
    assert await rd.PROVIDER.detect(_ctx()) is False


async def test_detect_short_circuits_before_http_when_no_key():
    # No respx mock registered → any HTTP would raise ConnectError; short-circuit must return False first.
    assert await rd.PROVIDER.detect(_ctx(api_key="")) is False
    assert await rd.PROVIDER.detect(_ctx(enabled=False)) is False


# -- command shape (the confirm-fills-from-inner invariant) ----------------------------------------

def test_command_tiers():
    cmds = {c.id: c for c in rd.PROVIDER.commands(_ctx())}
    assert cmds["radarr.search"].tier == 1
    assert cmds["radarr.add_movie"].tier == 2      # tier-2 IS the download confirm gate
    assert cmds["radarr.library"].tier == 1
    assert cmds["radarr.status"].tier == 1
    assert cmds["radarr.monitor"].tier == 2
    assert cmds["radarr.research"].tier == 2
    assert cmds["radarr.delete"].tier == 3         # destructive → keyboard confirm


def test_add_confirm_template_fields_are_required_slots():
    """Every {field} in a mutating command's confirm_template MUST be a required slot — else the template
    silently voids at the pre-exec gate (voice_approve._summarize_command reads only model-passed args)."""
    import string
    for c in rd.PROVIDER.commands(_ctx()):
        if not c.confirm_template:
            continue
        fields = {f for _, f, _, _ in string.Formatter().parse(c.confirm_template) if f}
        required = {s.name for s in c.params if s.required}
        # delete's template uses {title}; add uses {title}+{year} — all must be required.
        assert fields <= required, f"{c.id}: template fields {fields - required} are not required slots"


# -- search ----------------------------------------------------------------------------------------

@respx.mock
async def test_search_maps_candidates_and_in_library_flag():
    respx.get(f"{BASE}/api/v3/movie/lookup").mock(return_value=httpx.Response(200, json=[
        {"tmdbId": 693134, "title": "Dune: Part Two", "year": 2024, "id": 0},
        {"tmdbId": 438631, "title": "Dune", "year": 2021, "id": 12},   # id>0 ⇒ already added
    ]))
    r = await rd.search(_ctx(), query="Dune")
    got = json.loads(r.output)
    assert got[0] == {"tmdb_id": 693134, "title": "Dune: Part Two", "year": 2024, "in_library": False, "id": 0}
    assert got[1]["in_library"] is True and got[1]["id"] == 12


# -- add_movie -------------------------------------------------------------------------------------

def _add_mocks(*, tmdb=438631, in_lib=0, profiles=None, roots=None):
    respx.get(f"{BASE}/api/v3/movie/lookup/tmdb").mock(return_value=httpx.Response(200, json={
        "tmdbId": tmdb, "title": "Dune", "year": 2021, "titleSlug": "dune-438631",
        "images": [{"coverType": "poster"}], "id": in_lib}))
    respx.get(f"{BASE}/api/v3/qualityprofile").mock(return_value=httpx.Response(
        200, json=profiles if profiles is not None else [{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{BASE}/api/v3/rootfolder").mock(return_value=httpx.Response(
        200, json=roots if roots is not None else [{"id": 1, "path": "/movies", "accessible": True}]))
    return respx.post(f"{BASE}/api/v3/movie").mock(return_value=httpx.Response(201, json={"id": 99}))


@respx.mock
async def test_add_movie_happy_path_builds_correct_body():
    post = _add_mocks()
    # ★ wrong-match lock: pass a DIVERGENT model title — the POST body must carry the AUTHORITATIVE lookup
    # title ("Dune"), never the model-passed string. A regression that trusted the model title fails here.
    r = await rd.add_movie(_ctx(), tmdb_id=438631, title="WRONG MOVIE", year=1999)
    assert not r.error and "Added Dune (2021) to Radarr" in r.output   # label from lookup, not model args
    assert post.called
    body = json.loads(post.calls.last.request.content)
    assert body["title"] == "Dune" and body["year"] == 2021           # authoritative, NOT "WRONG MOVIE"/1999
    assert body["tmdbId"] == 438631 and body["titleSlug"] == "dune-438631"   # echoed authoritative fields
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/movies"
    assert body["monitored"] is True and body["minimumAvailability"] == "released"
    assert body["addOptions"] == {"searchForMovie": True, "monitor": "movieOnly"}


@respx.mock
async def test_add_movie_already_in_library_does_not_post():
    post = _add_mocks(in_lib=12)
    r = await rd.add_movie(_ctx(), tmdb_id=438631, title="Dune", year=2021)
    assert not r.error and "already in your Radarr library" in r.output
    assert not post.called


@respx.mock
async def test_add_movie_fails_loud_on_ambiguous_quality_profile():
    post = _add_mocks(profiles=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}])
    r = await rd.add_movie(_ctx(), tmdb_id=438631, title="Dune", year=2021)
    assert r.error and "quality profile" in r.error
    assert not post.called


@respx.mock
async def test_add_movie_fails_loud_on_ambiguous_root_folder():
    post = _add_mocks(roots=[{"id": 1, "path": "/movies", "accessible": True},
                             {"id": 2, "path": "/movies2", "accessible": True}])
    r = await rd.add_movie(_ctx(), tmdb_id=438631, title="Dune", year=2021)
    assert r.error and "download folder" in r.error
    assert not post.called


@respx.mock
async def test_add_movie_uses_configured_profile_name_and_root():
    post = _add_mocks(
        profiles=[{"id": 1, "name": "Any"}, {"id": 7, "name": "Ultra-HD"}],
        roots=[{"id": 1, "path": "/a", "accessible": True}, {"id": 2, "path": "/b", "accessible": True}])
    r = await rd.add_movie(_ctx(quality_profile="ultra-hd", root_folder_path="/b"),
                           tmdb_id=438631, title="Dune", year=2021)
    assert not r.error
    body = json.loads(post.calls.last.request.content)
    assert body["qualityProfileId"] == 7 and body["rootFolderPath"] == "/b"


async def test_add_movie_no_key():
    r = await rd.add_movie(_ctx(api_key=""), tmdb_id=1, title="x", year=2000)
    assert r.error and "isn't set up" in r.error


# -- status ----------------------------------------------------------------------------------------

@respx.mock
async def test_status_reports_queue_percent():
    respx.get(f"{BASE}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": [
        {"title": "Dune", "status": "downloading", "size": 1000, "sizeleft": 250, "timeleft": "00:10:00"}]}))
    r = await rd.status(_ctx(), query="dune")
    rec = json.loads(r.output)[0]
    assert rec["title"] == "Dune" and rec["percent"] == 75.0 and rec["time_left"] == "00:10:00"
