"""jellyfin.search — sequel-homophone normalization with the library-match false-positive guard.

The wife said "Pitch Perfect 2"; STT gave "Pitch Perfect to" → she got #1. Fix: search the digit form,
keep it ONLY when a returned title actually carries that number; else fall back to the spoken form."""
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from gabagent.commands.providers import jellyfin as J
from gabagent.config.models import GabAgentConfig, JellyfinConfig

BASE = "http://em:8096"


def _ctx():
    cfg = GabAgentConfig(api_key="t", jellyfin=JellyfinConfig(base_url=BASE, api_key="K"))
    return SimpleNamespace(config=cfg, room_id=None)


def _item(name, _id):
    return {"Id": _id, "Name": name, "ProductionYear": 2015, "CommunityRating": 7.0}


@respx.mock
async def test_homophone_resolves_to_the_numbered_title():
    """'Pitch Perfect to' → search 'Pitch Perfect 2' → library has it → use the digit results."""
    seen_terms = []

    def _resp(request):
        term = dict(httpx.QueryParams(request.url.query)).get("SearchTerm", "")
        seen_terms.append(term)
        if term == "Pitch Perfect 2":
            return httpx.Response(200, json={"Items": [_item("Pitch Perfect 2", "pp2")]})
        return httpx.Response(200, json={"Items": [_item("Pitch Perfect", "pp1")]})

    respx.get(f"{BASE}/Items").mock(side_effect=_resp)
    res = await J.search(_ctx(), query="Pitch Perfect to")
    out = json.loads(res.output)
    assert any(r["id"] == "pp2" for r in out)          # got the sequel
    assert "Pitch Perfect 2" in seen_terms             # the digit form was searched


@respx.mock
async def test_falls_back_when_no_numbered_title_exists():
    """'what are you waiting for' normalizes to '...4', but no title carries '4' → fall back to the
    spoken form so a non-sequel query isn't mangled."""
    seen_terms = []

    def _resp(request):
        term = dict(httpx.QueryParams(request.url.query)).get("SearchTerm", "")
        seen_terms.append(term)
        if term.endswith("4"):
            return httpx.Response(200, json={"Items": []})           # digit form finds nothing
        return httpx.Response(200, json={"Items": [_item("Waiting for Guffman", "wg")]})

    respx.get(f"{BASE}/Items").mock(side_effect=_resp)
    res = await J.search(_ctx(), query="what are you waiting for")
    out = json.loads(res.output)
    assert any(r["id"] == "wg" for r in out)           # fell back to the spoken query
    assert seen_terms[-1] == "what are you waiting for"  # raw form was the fallback search


@respx.mock
async def test_plain_title_searched_once_unchanged():
    seen_terms = []

    def _resp(request):
        seen_terms.append(dict(httpx.QueryParams(request.url.query)).get("SearchTerm", ""))
        return httpx.Response(200, json={"Items": [_item("The Matrix", "m")]})

    respx.get(f"{BASE}/Items").mock(side_effect=_resp)
    res = await J.search(_ctx(), query="The Matrix")
    assert json.loads(res.output)[0]["id"] == "m"
    assert seen_terms == ["The Matrix"]                # no extra normalized search
