"""Validate a router ladder against the live model catalog.

STATIC gaps only — a rung is DROPPED when:
  * its model id is not in the catalog (a wrong / deprovisioned name), or
  * the catalog says the model can't `function_calling` (it can't run the tool loop).

Plus-gating (`is_plus_only`) is NOT a static drop: the catalog carries no per-key entitlement
signal, so a false drop of a Plus rung the key CAN use would break routing. Such a rung is KEPT
with a WARN and left to the router's runtime `degraded` set (a Plus model the key lacks 4xxs on
first use → its backend is skipped for the rest of the session).

Fail-safe: an EMPTY catalog (never fetched / cache miss / outage) validates to a no-op — every
rung is kept. Validation can only ever NARROW a ladder when a catalog is actually present. And it
only checks rungs whose backend the catalog authoritatively describes (`backend == "gab"`); the
Anthropic-direct and local rungs are always kept unchecked here.
"""
from __future__ import annotations

from dataclasses import dataclass

from gabagent.models_catalog.catalog import ModelCatalog

# Backends the Aria/Gab catalog can speak to. A "claude"/"local" rung is served by a different
# client entirely, so this catalog is silent about it — keep it, unchecked.
CATALOG_BACKENDS = ("gab",)


@dataclass(frozen=True)
class RungVerdict:
    model: str
    backend: str
    keep: bool
    reason: str          # "" when kept-clean; the drop/warn explanation otherwise
    warn: bool = False   # kept, but worth surfacing (e.g. Plus-gated)
    checked: bool = True  # False → backend not in the catalog's authority, kept unchecked


def validate_ladder(
    rungs,
    catalog: ModelCatalog,
    *,
    catalog_backends=CATALOG_BACKENDS,
):
    """Return (kept_rungs, verdicts). `verdicts` covers EVERY input rung in order (for tooling);
    `kept_rungs` is the narrowed ladder to route on."""
    kept = []
    verdicts: list[RungVerdict] = []
    for r in rungs:
        backend = getattr(r, "backend", "gab") or "gab"
        # No catalog, or a backend the catalog isn't authoritative for → keep, unchecked.
        if not catalog or backend not in catalog_backends:
            kept.append(r)
            verdicts.append(RungVerdict(r.model, backend, keep=True, reason="", checked=False))
            continue
        info = catalog.get(r.model)
        if info is None:
            verdicts.append(RungVerdict(
                r.model, backend, keep=False,
                reason="not in catalog (unknown or deprovisioned model)"))
            continue
        if not info.function_calling:
            verdicts.append(RungVerdict(
                r.model, backend, keep=False,
                reason="model lacks function_calling (can't run the tool loop)"))
            continue
        if info.is_plus_only:
            verdicts.append(RungVerdict(
                r.model, backend, keep=True, warn=True,
                reason="is_plus_only — kept; runtime skips it if this key lacks Plus"))
        else:
            verdicts.append(RungVerdict(r.model, backend, keep=True, reason=""))
        kept.append(r)
    return kept, verdicts
