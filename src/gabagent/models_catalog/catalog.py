"""Fetch, cache, and load the Aria/Gab model catalog (`GET {base_url}/models`).

The endpoint is OpenAI-compatible but carries rich per-model metadata Gab adds: a `capabilities`
map (function_calling, embeddings, streaming, …), `context_window`, `max_output_tokens`, and
`is_plus_only`. We keep the raw records so the cache stays useful as new fields appear.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from gabagent.config.paths import data_dir

# Bump when the on-disk shape changes so a stale cache is ignored (→ empty → safe no-op) rather
# than mis-parsed.
CACHE_VERSION = 1
DEFAULT_TTL_SECS = 24 * 3600


def catalog_path() -> Path:
    return data_dir() / "models_catalog.json"


@dataclass(frozen=True)
class ModelInfo:
    """One model as the catalog describes it. `raw` keeps the full record for the diagnostic tool."""

    id: str
    function_calling: bool
    is_plus_only: bool
    context_window: int | None
    max_output_tokens: int | None
    capabilities: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_record(cls, m: dict) -> "ModelInfo":
        caps = m.get("capabilities") or {}
        return cls(
            id=(m.get("id") or m.get("name") or "").strip(),
            function_calling=bool(caps.get("function_calling")),
            is_plus_only=bool(m.get("is_plus_only")),
            context_window=m.get("context_window"),
            max_output_tokens=m.get("max_output_tokens"),
            capabilities=caps,
            raw=m,
        )


@dataclass(frozen=True)
class ModelCatalog:
    models: dict[str, ModelInfo]  # id -> info
    fetched_at: float             # unix ts of the fetch that produced this catalog
    source: str                   # "live" | "cache" | "empty"

    def get(self, model_id: str) -> ModelInfo | None:
        return self.models.get(model_id)

    def __bool__(self) -> bool:
        return bool(self.models)

    def __len__(self) -> int:
        return len(self.models)

    def age_secs(self, now: float | None = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.fetched_at)

    def is_stale(self, ttl_secs: float = DEFAULT_TTL_SECS, now: float | None = None) -> bool:
        return self.age_secs(now) > ttl_secs

    @classmethod
    def empty(cls) -> "ModelCatalog":
        return cls(models={}, fetched_at=0.0, source="empty")


def _parse_records(records) -> dict[str, ModelInfo]:
    out: dict[str, ModelInfo] = {}
    for m in (records or []):
        if not isinstance(m, dict):
            continue
        info = ModelInfo.from_record(m)
        if info.id:
            out[info.id] = info
    return out


def fetch_catalog(base_url: str, api_key: str, *, timeout: float = 15.0) -> ModelCatalog:
    """Live GET of the catalog. RAISES on network/HTTP error — only manual/setup callers use this;
    the routing path uses `load_catalog()`, which never raises."""
    import httpx

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    r = httpx.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    records = data.get("data") if isinstance(data, dict) else data
    return ModelCatalog(models=_parse_records(records), fetched_at=time.time(), source="live")


def write_cache(cat: ModelCatalog, path: Path | None = None) -> Path:
    p = path or catalog_path()
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": cat.fetched_at,
        "models": [mi.raw or _min_record(mi) for mi in cat.models.values()],
    }
    p.write_text(json.dumps(payload, indent=1))
    return p


def _min_record(mi: ModelInfo) -> dict:
    # Fallback record when `raw` is absent (e.g. a hand-built catalog in tests).
    return {
        "id": mi.id,
        "capabilities": mi.capabilities or {"function_calling": mi.function_calling},
        "is_plus_only": mi.is_plus_only,
        "context_window": mi.context_window,
        "max_output_tokens": mi.max_output_tokens,
    }


# In-process memo keyed on the cache file's mtime so per-turn assemble() calls don't re-read+parse
# the ~50KB JSON. Invalidates automatically when the file is rewritten (a refresh bumps mtime).
_memo: dict = {"mtime": None, "cat": None}


def load_catalog(path: Path | None = None) -> ModelCatalog:
    """Read the cached catalog for the ROUTING path. NEVER raises: a missing / unreadable / corrupt
    / version-mismatched cache returns an empty catalog, which validates to a no-op."""
    p = path or catalog_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ModelCatalog.empty()
    if _memo["mtime"] == mtime and _memo["cat"] is not None:
        return _memo["cat"]
    try:
        payload = json.loads(p.read_text())
        if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
            return ModelCatalog.empty()
        cat = ModelCatalog(
            models=_parse_records(payload.get("models")),
            fetched_at=float(payload.get("fetched_at") or 0.0),
            source="cache",
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return ModelCatalog.empty()
    _memo["mtime"] = mtime
    _memo["cat"] = cat
    return cat


def refresh_cache(base_url: str, api_key: str, *, timeout: float = 15.0, path: Path | None = None) -> ModelCatalog:
    """Fetch live + write the cache + prime the memo. RAISES on fetch error (caller reports it)."""
    cat = fetch_catalog(base_url, api_key, timeout=timeout)
    p = write_cache(cat, path)
    try:
        _memo["mtime"] = p.stat().st_mtime
        _memo["cat"] = cat
    except OSError:
        pass
    return cat
