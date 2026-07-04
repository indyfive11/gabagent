"""Tests for the credit-balance plugin (Aria/Gab /v1/credits) + the low-balance guard."""
import time
import types

import httpx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.credits import credits as C
from gabagent.credits import CreditBalance, get_balance, load_cached, low_balance_note


_LIVE = {
    "object": "credit_balance",
    "monthly_allotment": {"total": 2000, "used": 155, "remaining": 1845, "reset_date": "2026-07-29T00:00:00.000Z"},
    "purchased": {"available": 4342},
    "total_available": 6187,
    "is_plus": True,
}


class _Resp:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


def _cfg(tmp_path, **over):
    cfg = GabAgentConfig()
    cfg.api_key = over.pop("api_key", "gab_k")
    cfg.base_url = "https://gab.ai/v1"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "credits_path", lambda: tmp_path / "credits_cache.json")


# ── parsing ──────────────────────────────────────────────────────────────────

def test_from_record_parses_all_fields():
    b = CreditBalance.from_record(_LIVE, fetched_at=100.0)
    assert b.total_available == 6187
    assert b.monthly_remaining == 1845 and b.monthly_total == 2000 and b.monthly_used == 155
    assert b.purchased_available == 4342 and b.is_plus is True
    assert b.reset_date.startswith("2026-07-29")


def test_is_low():
    b = CreditBalance.from_record(_LIVE, fetched_at=0.0)
    assert b.is_low(10000) is True       # below threshold
    assert b.is_low(100) is False        # above threshold
    assert b.is_low(0) is False          # 0 disables
    assert b.is_low(-5) is False


# ── fetch / cache ────────────────────────────────────────────────────────────

def test_fetch_and_cache_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(_LIVE))
    bal = C.refresh("https://gab.ai/v1", "k")
    assert bal.total_available == 6187 and bal.source == "live"
    cached = load_cached()
    assert cached is not None and cached.total_available == 6187 and cached.source == "cache"


def test_load_cached_missing_and_corrupt(tmp_path):
    assert load_cached() is None                       # nothing written yet
    (tmp_path / "credits_cache.json").write_text("{bad json")
    assert load_cached() is None


def test_load_cached_version_mismatch(tmp_path):
    import json
    bal = CreditBalance.from_record(_LIVE, fetched_at=1.0)
    d = bal.to_cache()
    d["version"] = 999
    (tmp_path / "credits_cache.json").write_text(json.dumps(d))
    assert load_cached() is None


# ── get_balance (cache-first, fetch fallback) ────────────────────────────────

def test_get_balance_uses_fresh_cache_without_fetching(monkeypatch, tmp_path):
    C.write_cache(CreditBalance.from_record(_LIVE, fetched_at=time.time()))

    def _boom(*a, **k):
        raise AssertionError("must not fetch when cache is fresh")

    monkeypatch.setattr(httpx, "get", _boom)
    bal = get_balance(_cfg(tmp_path), max_age=300)
    assert bal is not None and bal.total_available == 6187


def test_get_balance_fetches_when_cache_stale(monkeypatch, tmp_path):
    C.write_cache(CreditBalance.from_record({**_LIVE, "total_available": 1}, fetched_at=0.0))  # ancient
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(_LIVE))
    bal = get_balance(_cfg(tmp_path), max_age=300, now=10_000.0)
    assert bal.total_available == 6187 and bal.source == "live"


def test_get_balance_falls_back_to_stale_on_fetch_error(monkeypatch, tmp_path):
    C.write_cache(CreditBalance.from_record({**_LIVE, "total_available": 42}, fetched_at=0.0))

    def _boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", _boom)
    bal = get_balance(_cfg(tmp_path), max_age=300, now=10_000.0)
    assert bal is not None and bal.total_available == 42   # stale-but-present beats nothing


def test_get_balance_none_without_key_or_cache(tmp_path):
    assert get_balance(_cfg(tmp_path, api_key="")) is None


# ── low_balance_note (the guard phrase) ──────────────────────────────────────

def test_low_balance_note_off_by_default(monkeypatch, tmp_path):
    C.write_cache(CreditBalance.from_record({**_LIVE, "total_available": 1}, fetched_at=time.time()))
    assert low_balance_note(_cfg(tmp_path)) == ""          # threshold 0 = off


def test_low_balance_note_fires_when_low(monkeypatch, tmp_path):
    C.write_cache(CreditBalance.from_record({**_LIVE, "total_available": 30}, fetched_at=time.time()))
    note = low_balance_note(_cfg(tmp_path, credits_low_threshold=100))
    assert "low on credits" in note and "30" in note


def test_low_balance_note_silent_when_above(monkeypatch, tmp_path):
    C.write_cache(CreditBalance.from_record(_LIVE, fetched_at=time.time()))
    assert low_balance_note(_cfg(tmp_path, credits_low_threshold=100)) == ""   # 6187 >= 100


def test_low_balance_note_safe_when_no_balance(tmp_path):
    # threshold set but no cache and no fetch possible → safe no-op, never raises
    assert low_balance_note(_cfg(tmp_path, api_key="", credits_low_threshold=100)) == ""
