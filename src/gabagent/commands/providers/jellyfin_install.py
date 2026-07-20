"""Jellyfin plugin installer — the Phase-2 reference retrofit.

Co-located with the runtime provider (`jellyfin.py`) so the plugin is the single source of truth for its own
install needs, but a SEPARATE module that is import-LIGHT: at module level it imports only stdlib + the
Layer-C contract — never httpx or the provider runtime — so the registry can load it before runtime deps
exist. The one network call (the reachability probe) imports httpx at CALL time.

Jellyfin is HTTP-only (a base URL + an API key), so its Manifest is empty — it exercises the
registry→check→configure→save path without any system-package mutation.
"""
from __future__ import annotations

from gabagent.install.contract import CheckReport, Manifest

_DEFAULT_BASE_URL = "http://localhost:8096"
_PROBE_TIMEOUT = 2.0  # one short attempt — a mistyped/firewalled base_url must never hang the wizard


class JellyfinInstaller:
    name = "jellyfin"
    manifest = Manifest()  # empty: no system pkgs / services / models — pure HTTP integration

    def check(self, cfg) -> CheckReport:
        """configured := an api_key is set (independent of reachability — Jellyfin's runtime detect() gates
        on reachability, not the key, so we report the two signals separately). reachable is a live probe,
        skipped (None) when there's no base_url. Never raises."""
        jc = getattr(cfg, "jellyfin", None)
        api_key = (getattr(jc, "api_key", "") or "") if jc is not None else ""
        base_url = (getattr(jc, "base_url", "") or "") if jc is not None else ""
        reachable = self._probe(base_url) if base_url else None
        notes: tuple[str, ...] = ()
        if api_key and reachable is False:
            notes = ("configured but the server didn't answer at base_url — check it's running/reachable",)
        elif not api_key:
            notes = ("no API key set — Jellyfin will stay dormant until configured (Dashboard → API Keys)",)
        return CheckReport(name=self.name, configured=bool(api_key), reachable=reachable, notes=notes)

    @staticmethod
    def _probe(base_url: str) -> bool | None:
        """Sync GET of the unauthenticated /System/Info/Public endpoint with a hard 2s cap. httpx is a core
        dep, imported at call-time to keep this module import-light. Any error ⇒ False (not reachable)."""
        import httpx  # call-time: descriptor module stays stdlib-only at import

        try:
            r = httpx.get(base_url.rstrip("/") + "/System/Info/Public", timeout=_PROBE_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False

    def configure(self, cfg, *, ask) -> bool:
        """Prompt for base URL + API key and write them into cfg.jellyfin. Returns True iff anything changed.
        No-clobber: if an api_key is already set, confirm before overwriting (a re-run must not silently stomp
        a hand-tuned value). The caller persists via save_config — this only mutates the in-memory model."""
        jc = getattr(cfg, "jellyfin", None)
        if jc is None:
            return False
        cur_url = getattr(jc, "base_url", "") or ""
        cur_key = getattr(jc, "api_key", "") or ""
        if cur_key and not ask.confirm("Jellyfin already has an API key — overwrite?", default=False):
            return False
        url = ask.prompt("Jellyfin base URL", default=cur_url or _DEFAULT_BASE_URL).strip()
        key = ask.prompt("Jellyfin API key (Dashboard → API Keys)", default=cur_key).strip()
        changed = False
        if url and url != cur_url:
            jc.base_url = url
            changed = True
        if key and key != cur_key:
            jc.api_key = key
            changed = True
        if changed:
            jc.enabled = True
        return changed


INSTALLER = JellyfinInstaller()
