"""Sonarr provider tests — respx-mocked against a FAKE host + FAKE key. Focus: the series divergences from
Radarr (seasons echoed, languageProfileId v3/v4 conditional, already-in-library = monitor-a-season not dupe)."""
import json
import types
import httpx
import respx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import sonarr as sn

BASE = "http://sonarr.test:8989"


def _ctx(**over):
    cfg = GabAgentConfig(api_key="test")
    cfg.sonarr.base_url = BASE
    cfg.sonarr.api_key = "k"
    for k, v in over.items():
        setattr(cfg.sonarr, k, v)
    return types.SimpleNamespace(config=cfg)


# -- detect + shape --------------------------------------------------------------------------------

@respx.mock
async def test_detect_true_and_wrong_appname_false():
    respx.get(f"{BASE}/api/v3/system/status").mock(return_value=httpx.Response(200, json={"appName": "Sonarr"}))
    assert await sn.PROVIDER.detect(_ctx()) is True


@respx.mock
async def test_detect_false_on_radarr_answering():
    respx.get(f"{BASE}/api/v3/system/status").mock(return_value=httpx.Response(200, json={"appName": "Radarr"}))
    assert await sn.PROVIDER.detect(_ctx()) is False


def test_command_tiers_and_confirm_required_slots():
    import string
    cmds = {c.id: c for c in sn.PROVIDER.commands(_ctx())}
    assert cmds["sonarr.add_show"].tier == 2 and cmds["sonarr.delete"].tier == 3
    for c in cmds.values():
        if not c.confirm_template:
            continue
        fields = {f for _, f, _, _ in string.Formatter().parse(c.confirm_template) if f}
        required = {s.name for s in c.params if s.required}
        assert fields <= required, f"{c.id}: {fields - required} not required (would void the confirm)"


# -- add_show --------------------------------------------------------------------------------------

def _lookup_series(tvdb=1399, in_lib=0):
    return respx.get(f"{BASE}/api/v3/series/lookup").mock(return_value=httpx.Response(200, json=[
        {"tvdbId": tvdb, "title": "Foundation", "year": 2021, "titleSlug": "foundation",
         "images": [], "seasons": [{"seasonNumber": 1, "monitored": True}], "id": in_lib}]))


def _profiles_roots(profiles=None, roots=None, langs=None, lang_status=200):
    respx.get(f"{BASE}/api/v3/qualityprofile").mock(return_value=httpx.Response(
        200, json=profiles if profiles is not None else [{"id": 4, "name": "HD-1080p"}]))
    respx.get(f"{BASE}/api/v3/rootfolder").mock(return_value=httpx.Response(
        200, json=roots if roots is not None else [{"id": 1, "path": "/tv", "accessible": True}]))
    respx.get(f"{BASE}/api/v3/languageprofile").mock(return_value=httpx.Response(
        lang_status, json=langs if langs is not None else []))


@respx.mock
async def test_add_show_happy_path_echoes_seasons_and_omits_langprofile_on_v4():
    _lookup_series()
    _profiles_roots(langs=[])   # v4: empty language-profile list ⇒ field omitted
    post = respx.post(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 42}))
    # ★ wrong-match lock: divergent model title → body must carry the authoritative lookup title.
    r = await sn.add_show(_ctx(), tvdb_id=1399, title="WRONG SHOW", year=1999)
    assert not r.error and "Added Foundation (2021) to Sonarr" in r.output
    body = json.loads(post.calls.last.request.content)
    assert body["title"] == "Foundation" and body["year"] == 2021   # authoritative, NOT "WRONG SHOW"/1999
    assert body["tvdbId"] == 1399 and body["seasons"] == [{"seasonNumber": 1, "monitored": True}]
    assert body["qualityProfileId"] == 4 and body["rootFolderPath"] == "/tv" and body["seasonFolder"] is True
    assert body["addOptions"] == {"searchForMissingEpisodes": True, "monitor": "all"}
    assert "languageProfileId" not in body   # ★ v4 omit


@respx.mock
async def test_add_show_includes_langprofile_on_v3():
    _lookup_series()
    _profiles_roots(langs=[{"id": 1, "name": "English"}])   # v3: language profiles present ⇒ include
    post = respx.post(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 42}))
    await sn.add_show(_ctx(), tvdb_id=1399, title="Foundation", year=2021)
    body = json.loads(post.calls.last.request.content)
    assert body["languageProfileId"] == 1   # ★ v3 include-when-resolvable


@respx.mock
async def test_add_show_omits_langprofile_on_404():
    _lookup_series()
    _profiles_roots(lang_status=404)
    post = respx.post(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(201, json={"id": 42}))
    await sn.add_show(_ctx(), tvdb_id=1399, title="Foundation", year=2021)
    assert "languageProfileId" not in json.loads(post.calls.last.request.content)


@respx.mock
async def test_add_show_already_in_library_is_monitor_intent_not_dupe_refusal():
    """★ Sonarr inverts Radarr: an existing series + 'add' is a legit monitor-a-new-season intent."""
    _lookup_series(in_lib=42)
    post = respx.post(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(201))
    r = await sn.add_show(_ctx(), tvdb_id=1399, title="Foundation", year=2021)
    assert not r.error and not post.called
    payload = json.loads(r.output)
    assert payload["already_in_library"] is True and payload["series_id"] == 42
    assert "sonarr.monitor" in payload["note"]


@respx.mock
async def test_add_show_fails_loud_on_ambiguous_root():
    _lookup_series()
    _profiles_roots(roots=[{"id": 1, "path": "/tv", "accessible": True},
                           {"id": 2, "path": "/tv2", "accessible": True}])
    post = respx.post(f"{BASE}/api/v3/series").mock(return_value=httpx.Response(201))
    r = await sn.add_show(_ctx(), tvdb_id=1399, title="Foundation", year=2021)
    assert r.error and "download folder" in r.error and not post.called


# -- monitor a season ------------------------------------------------------------------------------

@respx.mock
async def test_monitor_single_season():
    respx.get(f"{BASE}/api/v3/series/42").mock(return_value=httpx.Response(200, json={
        "id": 42, "title": "Foundation", "monitored": True,
        "seasons": [{"seasonNumber": 2, "monitored": False}, {"seasonNumber": 3, "monitored": False}]}))
    put = respx.put(f"{BASE}/api/v3/series/42").mock(return_value=httpx.Response(200, json={"id": 42}))
    r = await sn.monitor(_ctx(), series_id=42, title="Foundation", season=3, monitored=True)
    assert not r.error and "season 3" in r.output
    body = json.loads(put.calls.last.request.content)
    s3 = next(s for s in body["seasons"] if s["seasonNumber"] == 3)
    assert s3["monitored"] is True
    s2 = next(s for s in body["seasons"] if s["seasonNumber"] == 2)
    assert s2["monitored"] is False   # other seasons untouched
