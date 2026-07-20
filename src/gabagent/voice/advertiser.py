"""mDNS / DNS-SD advertiser for the brain (`_voice-brain._tcp`) — fail-soft + gated.

Publishes the brain's HTTP+SSE endpoint on the LAN so a satellite can discover the host WITHOUT a
hand-typed IP. Two properties are load-bearing:

  1. GATED on a non-loopback bind. A loopback brain (the historical default) has nothing to advertise —
     `start()` returns False and does nothing, so an unconfigured install behaves exactly as before.
  2. FAIL-SOFT on an absent optional dep. `zeroconf` is not required to run the brain; its absence (or any
     registration failure) degrades to "no discovery," never a crash (New-Module Deploy-Safety SOP: the
     guard covers BOTH the import AND the construction, so a host/satellite without zeroconf still serves).

Discovery is a PARALLEL ENHANCEMENT over a manual host, never a floor (CR-4). The advertised address is the
RESOLVED IPv4, never a `.local` name (CR-5): a satellite that can't resolve mDNS names still gets a usable
A-record it can connect to.

App-agnostic on the wire: the service TYPE is the Rob-blessed `_voice-brain._tcp.local.` — satellites match
on it, so it must never change.
"""
from __future__ import annotations

import socket

# Rob-blessed wire name (2026-07-20). Satellites discover on this exact string — DO NOT CHANGE.
_SERVICE_TYPE = "_voice-brain._tcp.local."


def _is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in ("", "127.0.0.1", "::1", "localhost")


def _build_properties(room_id: str) -> dict[bytes, bytes]:
    """The DNS-SD TXT record. `room_id` lets a multi-room satellite filter its browse to the right brain
    (empty = the default/only brain — a single-room install). `path` is a probe endpoint. No secrets ever
    go in TXT (it's broadcast in the clear)."""
    return {
        b"room_id": (room_id or "").strip().encode("utf-8"),
        b"path": b"/health",
    }


def _resolve_advertise_ip(host: str) -> str | None:
    """The IPv4 address to publish. An explicit LAN bind is used verbatim. For a bind-all (0.0.0.0 / ::)
    discover the primary-route source IP with a connected-but-silent UDP socket — the address a peer on the
    LAN would actually reach us on. Returns None if no address can be determined (→ caller skips)."""
    h = (host or "").strip()
    if h and h not in ("0.0.0.0", "::"):
        return h
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1 (RFC5737): no packet leaves — just selects egress iface
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


class BrainAdvertiser:
    """Lazy, fail-soft handle around a single zeroconf ServiceInfo registration. start()/stop() never raise."""

    def __init__(self) -> None:
        self._zc = None
        self._info = None

    def start(self, host: str, port: int, *, room_id: str = "", instance: str = "") -> bool:
        """Register `_voice-brain._tcp` for (resolved-ip, port), TXT carrying `room_id`. Returns True if now
        advertising, False if skipped — loopback bind, `zeroconf` absent, no resolvable address, or a
        registration error. Never raises: a discovery failure must not take down the brain."""
        if _is_loopback(host):
            return False  # nothing to advertise on loopback → historical no-op
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return False  # optional dep absent → degrade to no discovery (fail-soft)
        ip = _resolve_advertise_ip(host)
        if not ip:
            return False
        zc = None
        try:
            name = instance.strip() or socket.gethostname().split(".")[0] or "brain"
            info = ServiceInfo(
                _SERVICE_TYPE,
                f"{name}.{_SERVICE_TYPE}",
                addresses=[socket.inet_aton(ip)],  # A-record: resolved IPv4, not a .local name (CR-5)
                port=int(port),
                properties=_build_properties(room_id),  # TXT: room_id (satellite filter) + probe path
                server=f"{name}.local.",
            )
            zc = Zeroconf()
            zc.register_service(info)
        except Exception:
            if zc is not None:  # best-effort teardown of a half-open Zeroconf
                try:
                    zc.close()
                except Exception:
                    pass
            return False  # construction/registration failure is non-fatal (fail-soft, per SOP)
        self._zc, self._info = zc, info
        return True

    def stop(self) -> None:
        """Unregister + close. Idempotent and never raises (safe in a `finally`)."""
        zc, info = self._zc, self._info
        self._zc = self._info = None
        if zc is None:
            return
        try:
            if info is not None:
                zc.unregister_service(info)
            zc.close()
        except Exception:
            pass
