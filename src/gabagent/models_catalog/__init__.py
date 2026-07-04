"""Live model catalog (Aria/Gab `/v1/models`) — validate the router ladder against reality.

The router assembles its ladder from configured model NAMES; this plugin lets it check the
**gab-backend** rungs against what the API actually serves on this key, dropping a wrong /
deprovisioned name or a non-tool-capable model before it wastes a call and a bad turn.

Design (per the hardware-generalization SOP — detect-once-and-write, never a per-startup probe):
  * a setup / `gab --models` step FETCHES the catalog and WRITES a cache (smart, explicit);
  * the running router only READS the cache and NARROWS the ladder (dumb, cheap, per-turn);
  * a missing / stale / empty cache is always safe — validation degrades to a no-op and the
    configured ladder is used unchanged (an outage can never break routing).

Scope: the Gab catalog authoritatively describes only gab.ai-served models, so validation touches
only `backend == "gab"` rungs. Anthropic-direct ("claude") rungs and the local Ollama rung are
validated by their own configuration/runtime paths and are always kept here. Plus-gated models
(`is_plus_only`) are kept with a WARN — there is no per-key entitlement signal in the catalog, so a
Plus rung the key lacks is left to the router's runtime `degraded` set (one 4xx → skipped/session).
"""
from gabagent.models_catalog.catalog import (
    ModelCatalog,
    ModelInfo,
    catalog_path,
    load_catalog,
    refresh_cache,
)
from gabagent.models_catalog.validate import RungVerdict, validate_ladder

__all__ = [
    "ModelCatalog",
    "ModelInfo",
    "RungVerdict",
    "catalog_path",
    "load_catalog",
    "refresh_cache",
    "validate_ladder",
]
