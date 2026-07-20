"""Plugin-installer registry — Layer C.

Discovery is an EXPLICIT hard-import list, mirroring the runtime provider registry
(`commands/providers/__init__.py`: `from … import PROVIDER as _x` → `PROVIDERS = [...]`). This is a
deliberate reversal of INSTALL_PLAN §5's originally-logged pkgutil/entry-points auto-discovery, blessed in the
2026-07-20 Phase-2 audit on merits:
  - the provider registry is the exact working precedent in this tree; auto-discovery has none;
  - a hard import fails LOUD at import/test time — correct for a small curated internal list that runs on a
    full `git clone` (the fail-soft New-Module SOP is for the rsync-synced Pi satellite, not this path);
  - no import-side-effect / import-ordering magic to reason about.
The plan-doc reversal itself is the maintainer's to ratify; the engineering is settled.

To register a plugin's installer: add one `from ….<plugin>_install import INSTALLER as _<plugin>` line and
append it to INSTALLERS. Each `*_install` module is import-LIGHT (stdlib + this contract only; the reachability
probe imports httpx at call-time), so loading the registry never drags in a provider's runtime deps.
"""
from __future__ import annotations

from gabagent.commands.providers.jellyfin_install import INSTALLER as _jellyfin
from gabagent.install.contract import PluginInstaller

# The curated set of plugin installers. One line per plugin; later phases add radarr/sonarr/tidal/moviescout.
INSTALLERS: list[PluginInstaller] = [_jellyfin]


def load_installers() -> list[PluginInstaller]:
    """Return the registered plugin installers (a fresh list so callers can't mutate the registry)."""
    return list(INSTALLERS)
