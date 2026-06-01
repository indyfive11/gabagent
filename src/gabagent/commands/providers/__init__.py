"""First-party capability providers (trusted Python; may use any backend).

Populated in later phases (Jellyfin browser-play, MPRIS, app-launch, system). Discovery
detects which are available on the host. Third-party capabilities arrive as attested skill
plugins instead, never as providers.
"""
from __future__ import annotations
from gabagent.commands.providers.base import CapabilityProvider

PROVIDERS: list[CapabilityProvider] = []
