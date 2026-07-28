"""Layer-C voice-host role step — write the mDNS advertise opt-in.

The last §10d piece. A full-voice-host install must flip gabagent's `voice_advertise` so the brain
broadcasts `_voice-brain._tcp` and a satellite can discover it without a hand-typed IP. Because
`voice_advertise` is a gabagent `settings.json` field, this write is **Layer C's** — Layer B (voice-agent,
which cannot `import gabagent`) invokes it cross-process via the `gabagent-install --enable-voice-host`
console script.

THE GRAFT (maintainer-ratified 2026-07-27 — a §10d/§6 amendment). The flag is INERT on a loopback bind: the
runtime advertiser skips loopback (`voice/server.py` gates `advertiser.start` on the bind host, and
`advertiser._is_loopback` no-ops there), so `voice_advertise:true` with the default `voice_host=127.0.0.1`
broadcasts nothing AND makes the satellite-side §10d remedy text lie ("the flag is set" while the wire is
empty). So this step co-writes a non-loopback `voice_host` and **REFUSES on loopback** rather than shipping a
silent no-op. Division of labor: Layer B DETECTS the LAN IP (primary-route interface) and passes it via
`--host`; Layer C VALIDATES it is non-loopback and WRITES; the runtime READS. `advertiser._is_loopback` is
imported as the SINGLE SOURCE OF TRUTH so the installer's notion of loopback can never drift from the
runtime gate.

Import-light (stdlib + the loopback predicate only) so the installer registry never drags in voice runtime
deps. `save_config` is the CALLER's — this step only mutates the passed cfg (mirrors the plugin contract).
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass

from gabagent.voice.advertiser import _is_loopback


class RefuseAdvertise(Exception):
    """The effective voice_host cannot carry an mDNS advertisement (loopback, unset, or a wildcard/any
    bind). Raised BEFORE any mutation, so the caller's cfg is untouched on refusal."""


class LoopbackRefused(RefuseAdvertise):
    """The effective voice_host is loopback — enabling voice_advertise would silently broadcast nothing."""


def _is_wildcard(host: str) -> bool:
    """A wildcard/any-address bind (INADDR_ANY / IN6ADDR_ANY) — a real bind target, but never a specific
    LAN address the advertiser can publish, so it must be refused distinctly from loopback."""
    return (host or "").strip() in ("0.0.0.0", "::")


@dataclass(frozen=True)
class AdvertiseResult:
    """What the write did. `changed` drives whether the caller persists; `notes` carries non-fatal
    diagnostics (e.g. the `no-mdns` reason when the optional zeroconf dep is absent)."""
    changed: bool
    host: str
    room_id: str | None
    zeroconf_present: bool
    notes: tuple[str, ...] = ()


def enable_voice_advertise(cfg, *, host: str | None = None, room_id: str | None = None) -> AdvertiseResult:
    """Enable LAN mDNS advertising on `cfg` (mutates in place; the caller owns `save_config`).

    Effective host = `host` (the Layer-B-detected LAN IP) or the existing `cfg.voice_host`. If it is
    loopback, raise `LoopbackRefused` WITHOUT mutating cfg. Otherwise write `voice_host` (= effective) +
    `voice_advertise=True` (and `voice_room_id` if a non-empty `room_id` is given). Idempotent: returns
    `changed=False` when the config already matched, so a re-run writes no churn.
    """
    supplied = (host or "").strip()
    current = (getattr(cfg, "voice_host", "") or "").strip()

    # A wildcard/any bind is never a specific advertisable address — refuse a supplied one outright (a
    # direct `--host 0.0.0.0`; the integrated Layer-B path already rejects it pre-seam).
    if supplied and _is_wildcard(supplied):
        raise RefuseAdvertise(
            f"--host {supplied!r} is a wildcard/any-address bind, not a specific LAN address the advertiser "
            "can publish. Pass the brain's concrete LAN IP."
        )

    notes: list[str] = []
    # THE GRAFT, fork (a): an explicit NON-LOOPBACK `voice_host` is the operator's deliberate bind and WINS
    # over detection — `voice_host` is the server's listen address (config.models), not just the advertised
    # one, so enabling advertising must never silently relocate where the brain listens. A supplied `--host`
    # only FILLS a loopback/unset bind; a non-loopback supplied host that diverges from the configured bind
    # WARNS rather than clobbers (the operator re-runs deliberately if they truly want to rebind).
    if not _is_loopback(current) and not _is_wildcard(current):
        effective = current
        if supplied and not _is_loopback(supplied) and supplied != current:
            notes.append(
                f"divergence: --host {supplied} differs from the configured voice_host {current}; keeping the "
                "configured bind (an explicit non-loopback voice_host wins over detection). Edit voice_host "
                "and re-run if you intend to rebind."
            )
    else:
        effective = supplied  # current is loopback/unset/wildcard → the detected/supplied host fills it

    if _is_wildcard(effective):
        raise RefuseAdvertise(
            f"voice_host is a wildcard/any-address bind ({effective!r}); set a concrete LAN IP (via --host) "
            "so the advertiser can publish a reachable address."
        )
    if _is_loopback(effective):
        raise LoopbackRefused(
            f"effective voice_host is loopback ({effective!r}); the mDNS advertiser only broadcasts on a "
            "non-loopback bind, so enabling voice_advertise here would broadcast nothing. Pass "
            "--host <LAN_IP> (the brain's LAN address) so a satellite can actually discover it."
        )

    changed = False
    if current != effective:
        cfg.voice_host = effective
        changed = True
    if not getattr(cfg, "voice_advertise", False):
        cfg.voice_advertise = True
        changed = True
    rid = (room_id or "").strip()
    if rid and getattr(cfg, "voice_room_id", "") != rid:
        cfg.voice_room_id = rid
        changed = True

    zc_present = importlib.util.find_spec("zeroconf") is not None
    if not zc_present:
        # Mirrors §10d's `no-mdns` reason: the write succeeds but discovery stays dark until the optional
        # zeroconf dep is present in the brain's environment. Non-fatal — the flag is still recorded.
        notes.append(
            "no-mdns: the optional `zeroconf` dependency is not installed in this environment, so the brain "
            "will advertise nothing until it is added. Install `zeroconf` in the brain's venv to light "
            "discovery up."
        )
    return AdvertiseResult(
        changed=changed, host=effective, room_id=(rid or None), zeroconf_present=zc_present, notes=tuple(notes)
    )
