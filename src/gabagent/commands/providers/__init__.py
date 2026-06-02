"""First-party capability providers (trusted Python; may use any backend).

Populated in later phases (Jellyfin browser-play, MPRIS, app-launch, system). Discovery
detects which are available on the host. Third-party capabilities arrive as attested skill
plugins instead, never as providers.
"""
from __future__ import annotations
from gabagent.commands.providers.base import CapabilityProvider
from gabagent.commands.providers.jellyfin import PROVIDER as _jellyfin
from gabagent.commands.providers.tidal import PROVIDER as _tidal
from gabagent.commands.providers.mpris import PROVIDER as _mpris
from gabagent.commands.providers.system import PROVIDER as _system
from gabagent.commands.providers.applaunch import PROVIDER as _applaunch
from gabagent.commands.providers.desktop import PROVIDER as _desktop

PROVIDERS: list[CapabilityProvider] = [_jellyfin, _tidal, _mpris, _system, _applaunch, _desktop]
