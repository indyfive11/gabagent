"""A scoped Media Inventory: enumerate every media source the brain can SEE (local + remote, all
providers) as a uniform `MediaSource`, so the brain can reason about them without conflating "what I can
observe" with "what I may control."

The governing rule (see the architecture plan): the brain only AUTO-ducks/controls media it judges LOCAL to
this device; media on another device/room is visible (for future explicit commands like "stop all movies
outside HomeServer") but NEVER touched automatically. This is what kills the cross-device control bug — a
remote Jellyfin session can no longer drive `/media/state.kind` or get auto-paused when the user speaks.

Providers contribute sources via an optional `async sources(ctx) -> list[MediaSource]` (duck-typed; a provider
without it simply contributes nothing). `inventory(ctx)` aggregates them with the same never-raise / one-bad-
provider-can't-break-discovery discipline as `commands/discovery.py`.
"""
from __future__ import annotations
import asyncio
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# The DeviceId the brain presents to Jellyfin when it launches a movie (jellyfin auth header). A /Sessions
# entry carrying this id is unambiguously ours → local. Keep in sync with the value sent at auth.
OWN_JELLYFIN_DEVICE_ID = "gabagent-voice"


@dataclass
class MediaSource:
    """One playable media source, anywhere the brain can see it.

    `owned`   = the brain launched/controls it on THIS box (Mopidy music, a movie we opened).
    `locality`= "local" (this device's audio output), "remote" (another device/room), or "unknown".
    Only `auto_scoped` sources may be auto-controlled; only `audible` ones affect the local mic loop.
    Control callables are populated in Phase 2 for foreign controllable sessions; owned sources are ducked
    via the existing provider paths, so they stay None here on the Phase-1 auto path."""
    provider: str                       # "tidal" | "jellyfin" | ...
    kind: str                           # "audio" | "video"
    state: str                          # "playing" | "paused" | "stopped"
    owned: bool = False
    locality: str = "unknown"           # "local" | "remote" | "unknown"
    device_name: str = ""
    device_id: str = ""
    endpoint: str = ""                  # client RemoteEndPoint ("ip:port"), when known
    session_key: str = ""               # provider-specific id (Jellyfin session Id, …)
    title: str = ""
    pause: Optional[Callable[..., Awaitable[bool]]] = None
    resume: Optional[Callable[..., Awaitable[bool]]] = None

    @property
    def is_local(self) -> bool:
        return self.locality == "local"

    @property
    def audible(self) -> bool:
        """Local media that is producing or holding sound on this box (so it's in the mic's AEC loop)."""
        return self.is_local and self.state in ("playing", "paused")

    @property
    def auto_scoped(self) -> bool:
        """The brain may AUTO-control this via its provider path: brain-owned AND local."""
        return self.owned and self.is_local


def _local_device_name(ctx) -> str:
    name = (getattr(getattr(ctx, "config", None), "local_device", "") or "").strip()
    if name:
        return name
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _is_loopback(endpoint: str) -> bool:
    """endpoint is a client RemoteEndPoint like '127.0.0.1:53124' or '[::1]:53124' or '::1'."""
    if not endpoint:
        return False
    host = endpoint.strip()
    if host.startswith("["):                       # [::1]:port
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:                      # ipv4:port
        host = host.rsplit(":", 1)[0]
    host = host.strip("[]").lower()
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def judge_locality(ctx, *, device_id: str = "", device_name: str = "", endpoint: str = "") -> str:
    """Conservative local/remote verdict for an observed (unowned) session. Default REMOTE when unprovable,
    so we never auto-control something we can't confirm is on this box."""
    if device_id and device_id == OWN_JELLYFIN_DEVICE_ID:
        return "local"
    if endpoint and _is_loopback(endpoint):
        return "local"
    ld = _local_device_name(ctx)
    if ld and device_name and device_name.strip().lower() == ld.strip().lower():
        return "local"
    return "remote"


async def inventory(ctx) -> list[MediaSource]:
    """All media sources visible right now, across providers. Never raises; a slow/broken provider
    contributes nothing rather than aborting the inventory (mirrors commands/discovery.py)."""
    try:
        from gabagent.commands.providers import PROVIDERS
        providers = PROVIDERS
    except Exception:
        return []

    async def _one(prov):
        fn = getattr(prov, "sources", None)
        if fn is None:
            return []
        try:
            return list(await fn(ctx)) or []
        except Exception:
            return []

    results = await asyncio.gather(*[_one(p) for p in providers], return_exceptions=True)
    out: list[MediaSource] = []
    for r in results:
        if isinstance(r, list):
            out.extend(s for s in r if isinstance(s, MediaSource))
    return out


def local_audible(sources: list[MediaSource]) -> list[MediaSource]:
    return [s for s in sources if s.audible]


def auto_scoped(sources: list[MediaSource]) -> list[MediaSource]:
    return [s for s in sources if s.auto_scoped]
