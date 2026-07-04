"""Fetch, cache, and read the Aria/Gab credit balance (`GET {base_url}/credits`).

Live-verified contract (2026-07-04):
    GET {base_url}/credits →
      {"object":"credit_balance",
       "monthly_allotment":{"total":2000,"used":155,"remaining":1845,"reset_date":"…"},
       "purchased":{"available":4342},
       "total_available":6187,
       "is_plus":true}

`total_available` is the spendable pool (monthly remaining + purchased). The balance changes on
every spend, so this uses a SHORT-TTL disk cache — the low-balance guard reads the cache (never
polls per call), and `gab --credits` forces a fresh fetch. A missing/stale/failed read degrades to
None so the guard is a safe no-op (never blocks or warns on absent data).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from gabagent.config.paths import data_dir

CACHE_VERSION = 1
# How long a cached balance is considered fresh for the low-balance guard. Short — the balance moves
# with every spend — but long enough that a burst of confirms doesn't re-poll. `gab --credits` bypasses it.
DEFAULT_TTL_SECS = 300


def credits_path() -> Path:
    return data_dir() / "credits_cache.json"


@dataclass(frozen=True)
class CreditBalance:
    total_available: int          # spendable pool (monthly remaining + purchased)
    monthly_remaining: int
    monthly_total: int
    monthly_used: int
    purchased_available: int
    is_plus: bool
    reset_date: str
    fetched_at: float
    source: str = "live"          # "live" | "cache"

    @classmethod
    def from_record(cls, d: dict, *, fetched_at: float, source: str = "live") -> "CreditBalance":
        ma = d.get("monthly_allotment") or {}
        pur = d.get("purchased") or {}
        return cls(
            total_available=int(d.get("total_available", 0) or 0),
            monthly_remaining=int(ma.get("remaining", 0) or 0),
            monthly_total=int(ma.get("total", 0) or 0),
            monthly_used=int(ma.get("used", 0) or 0),
            purchased_available=int(pur.get("available", 0) or 0),
            is_plus=bool(d.get("is_plus")),
            reset_date=str(ma.get("reset_date", "") or ""),
            fetched_at=fetched_at,
            source=source,
        )

    def age_secs(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.fetched_at

    def is_low(self, threshold: int) -> bool:
        """True when a positive threshold is set and the spendable pool is below it. threshold <= 0
        disables the check (the config-generalization default: unconfigured never warns)."""
        return threshold > 0 and self.total_available < threshold

    def to_cache(self) -> dict:
        return {
            "version": CACHE_VERSION,
            "fetched_at": self.fetched_at,
            "balance": {
                "total_available": self.total_available,
                "monthly_remaining": self.monthly_remaining,
                "monthly_total": self.monthly_total,
                "monthly_used": self.monthly_used,
                "purchased_available": self.purchased_available,
                "is_plus": self.is_plus,
                "reset_date": self.reset_date,
            },
        }


def fetch_balance(base_url: str, api_key: str, timeout: float = 15.0) -> CreditBalance:
    """GET the live balance. RAISES on network/HTTP error."""
    import httpx

    url = f"{base_url.rstrip('/')}/credits"
    resp = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    resp.raise_for_status()
    return CreditBalance.from_record(resp.json(), fetched_at=time.time(), source="live")


def write_cache(bal: CreditBalance, path: Path | None = None) -> None:
    p = path or credits_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(bal.to_cache(), indent=2), encoding="utf-8")
    except OSError:
        pass


def load_cached(path: Path | None = None) -> CreditBalance | None:
    """The last-written balance, or None if missing/corrupt/version-mismatch. NEVER raises."""
    p = path or credits_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("version") != CACHE_VERSION:
        return None
    b = d.get("balance") or {}
    try:
        return CreditBalance(
            total_available=int(b.get("total_available", 0) or 0),
            monthly_remaining=int(b.get("monthly_remaining", 0) or 0),
            monthly_total=int(b.get("monthly_total", 0) or 0),
            monthly_used=int(b.get("monthly_used", 0) or 0),
            purchased_available=int(b.get("purchased_available", 0) or 0),
            is_plus=bool(b.get("is_plus")),
            reset_date=str(b.get("reset_date", "") or ""),
            fetched_at=float(d.get("fetched_at", 0) or 0),
            source="cache",
        )
    except (TypeError, ValueError):
        return None


def refresh(base_url: str, api_key: str, timeout: float = 15.0, path: Path | None = None) -> CreditBalance:
    """Force a live fetch and write the cache. RAISES on fetch error."""
    bal = fetch_balance(base_url, api_key, timeout=timeout)
    write_cache(bal, path)
    return bal


def low_balance_note(cfg, *, now: float | None = None) -> str:
    """A brief heads-up phrase ("you're low on credits — N left") when the guard is tripped, else "".
    Reads the cached/live balance via get_balance; a safe no-op (returns "") when the threshold is 0/unset,
    there's no balance, or anything fails — the guard must never break a spend the user asked for."""
    thr = int(getattr(cfg, "credits_low_threshold", 0) or 0)
    if thr <= 0:
        return ""
    try:
        bal = get_balance(cfg, now=now)
    except Exception:
        return ""
    if bal is not None and bal.is_low(thr):
        return f"you're low on credits — {bal.total_available} left"
    return ""


def get_balance(
    cfg,
    *,
    max_age: float = DEFAULT_TTL_SECS,
    allow_fetch: bool = True,
    now: float | None = None,
) -> CreditBalance | None:
    """The balance for the low-balance guard: return a fresh-enough cached value; otherwise fetch live
    (writing the cache) when `allow_fetch` and a key exists. On a fetch failure fall back to any cached
    value (even stale). Returns None only when there's no key AND no cache. NEVER raises — the guard must
    be a safe no-op on any failure."""
    cached = load_cached()
    if cached is not None and cached.age_secs(now) <= max_age:
        return cached
    api_key = getattr(cfg, "api_key", "") or ""
    if allow_fetch and api_key:
        try:
            return refresh(getattr(cfg, "base_url", "https://gab.ai/v1"), api_key)
        except Exception:
            return cached  # stale-but-present beats nothing for a rough "are you low" check
    return cached
