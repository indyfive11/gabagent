"""Deterministic weighting + the adaptive pressure bands for Tier-0 — pure functions over Fact
metadata (no LLM, no IO). Coefficients and the band curve are internal defaults; their RELATIVE
magnitudes are what matter, tuned later on real logs.
"""
from __future__ import annotations

import math
from collections import namedtuple

from gabagent.tmi.facts import Fact

# Weight coefficients. explicit dominates (a direct instruction outranks mere repetition); cross-room
# is the signal only TMI can compute; volatility is reserved (facts carry 0.0 until a producer exists).
W_RECENCY = 1.0
W_FREQUENCY = 1.0
W_CROSSROOM = 1.5
W_EXPLICIT = 3.0
W_VOLATILITY = 1.0

# Base admit threshold in score units. A candidate must clear admit_mult * T_BASE for its band.
T_BASE = 3.0
# Prune floor sits at this fraction of the band's admit bar — strictly below it, so a just-admitted
# fact has margin and does not flap out next cycle (stateless hysteresis).
PRUNE_FRACTION = 0.5

Band = namedtuple("Band", "name admit_bar halflife_days prune_floor")

# (size_upper_exclusive, name, admit_multiplier, decay_halflife_days). As Tier 0 fills, the admit bar
# rises and the half-life shortens (old facts fade faster under pressure). Assumes soft_cap 20 /
# hard_cap 50; the prune floor is derived (PRUNE_FRACTION * admit_bar), so it hardens with the band too.
_BAND_DEFS = [
    (20, "relaxed", 1.0, 30),
    (30, "medium", 1.3, 21),
    (40, "firm", 1.6, 14),
    (50, "strict", 2.0, 10),
]


def band_for(size: int) -> Band:
    """The single band a cycle operates in, chosen ONCE from the current fact-count (no mid-cycle
    recompute — that was the flapping bug). At/over the last boundary, strict rules apply."""
    for upper, name, mult, hl in _BAND_DEFS:
        if size < upper:
            admit = mult * T_BASE
            return Band(name, admit, hl, PRUNE_FRACTION * admit)
    _, name, mult, hl = _BAND_DEFS[-1]
    admit = mult * T_BASE
    return Band(name, admit, hl, PRUNE_FRACTION * admit)


def decay(last_seen: float, now: float, halflife_days: float) -> float:
    """Recency weight in (0,1]: 1.0 now, 0.5 one half-life ago. halflife_days is the BAND's."""
    if halflife_days <= 0:
        return 1.0
    age_days = max(0.0, (now - last_seen) / 86400.0)
    return 0.5 ** (age_days / halflife_days)


def score(f: Fact, now: float, halflife_days: float) -> float:
    """Total weight. Higher = more deserving of a shared Tier-0 slot. Pure over the fact's metadata."""
    return (
        W_RECENCY * decay(f.last_seen, now, halflife_days)
        + W_FREQUENCY * math.log1p(max(0, f.hits))
        + W_CROSSROOM * len(set(f.sources))
        + W_EXPLICIT * (1.0 if f.explicit else 0.0)
        - W_VOLATILITY * max(0.0, f.volatility)
    )
