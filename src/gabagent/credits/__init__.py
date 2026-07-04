"""Credit-balance plugin — the Aria/Gab `/v1/credits` capability.

`gab --credits` reads + prints the live balance (and seeds a short-TTL cache). The low-balance guard
reads that cache (never polls per call) so a credit-spending tool (image-gen, video) can add a brief
heads-up when the spendable pool is below the configured `credits_low_threshold`. Default threshold 0
⇒ the guard is a no-op and an unconfigured install behaves exactly as before.
"""
from __future__ import annotations

from .credits import (
    CreditBalance,
    credits_path,
    fetch_balance,
    get_balance,
    load_cached,
    low_balance_note,
    refresh,
    write_cache,
)

__all__ = [
    "CreditBalance",
    "credits_path",
    "fetch_balance",
    "get_balance",
    "load_cached",
    "low_balance_note",
    "refresh",
    "write_cache",
]
