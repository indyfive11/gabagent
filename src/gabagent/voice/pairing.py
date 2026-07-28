"""Voice-brain PAIRING — the CLAIM state machine for token auto-provisioning.

A fresh voice front end obtains the brain's bearer token over the wire during a short, human-authorized
window, replacing the hand-typed token. The client-facing `POST /pair` contract is front-end-AGNOSTIC
(any conforming front end can pair); the operator side (`gab pairvoiceagent`, the admin routes) is
brain-private and NOT part of the published contract.

TWO decoupled lifecycles (the GA↔VAC R1–R5 consensus — see comms/2026-07-27/…pairing…):

  - WINDOW  (operator-facing): opened by `gab pairvoiceagent`; closes on the operator ACCEPT (no new
            candidates after) or on timeout.
  - CLAIM   (satellite-facing): the accepted candidate becomes a retrievable *claim* with its OWN short
            TTL, replay-gated on ``{peer-IP + claim_secret}`` — so a dropped `202`/`200` recovers.

Security model (deliberately honest — the trust anchor is a human at the brain, not the network):

  - Transport is cleartext HTTP. Pairing removes the TYPING, not the on-wire exposure: an on-PATH sniffer
    still reads the token here and on every call afterward (→ TLS territory, out of scope). What pairing
    *does* defend, and against whom:
      * GUESSING (switched LAN, no sniff): retrieval needs the SERVER-generated high-entropy
        ``claim_secret``, so even a weak/guessable client-chosen ``client_id`` can't be guessed into a
        token. The server-side secret guarantees retrieval entropy regardless of how a third-party front
        end picks its ``client_id`` — a sloppy front end can't foot-gun the fleet.
      * OFF-PATH spoofing: a candidate is pinned to the source IP that registered it; a different IP
        presenting the same ``client_id`` is rejected, never merged.
      * RACE-TO-GRAB: only the ONE candidate a human explicitly ACCEPTS becomes retrievable; a resident
        scanner cannot win by being faster.

This module is pure + synchronous (no I/O, no asyncio) so the state machine is unit-testable in
isolation. In single-threaded asyncio a synchronous critical section is already atomic (no interleave
without an await); the server layer additionally serialises calls under one ``asyncio.Lock`` to honour
the agreed at-most-one-issuance invariant explicitly and stay robust if an await is ever introduced.
"""
from __future__ import annotations

import hmac
import time as _time
from dataclasses import dataclass

from installkit.secrets import generate_token

__all__ = ["PairingState", "PairOutcome", "sanitize_label", "MIN_CLIENT_ID_LEN"]

# The client_id is the client's stable idempotency key. The server can't verify entropy, only length;
# this floor rejects an obviously-weak id (e.g. "raspi") while a conforming client ships a >=128-bit
# random one. It is a proxy, NOT the real guard — the server-generated claim_secret is (see module doc).
MIN_CLIENT_ID_LEN = 20

DEFAULT_WINDOW_TTL = 300.0   # operator pairing window (5 min); re-run pairvoiceagent to reopen.
DEFAULT_CLAIM_TTL = 30.0     # accepted-candidate retrieval window: >=30 poll retries at ~1s cadence.

# C0 (0x00–0x1F) + DEL (0x7F) + C1 (0x80–0x9F): the control ranges an attacker-supplied label could use
# for ANSI/newline injection to forge a row or overwrite the observed-IP column on the operator TTY.
_CTRL = set(range(0x00, 0x20)) | {0x7F} | set(range(0x80, 0xA0))


def sanitize_label(raw: object, *, max_len: int = 32) -> str:
    """Strip C0/C1 control bytes and clamp length, BEFORE a pairing candidate's label is rendered to the
    operator TTY. The label is attacker-supplied and untrusted — it is decoration only; the observed
    peer-IP (added by the server, not the client) is the identifier the human actually trusts."""
    if not isinstance(raw, str):
        return ""
    cleaned = "".join(ch for ch in raw if ord(ch) not in _CTRL).strip()
    return cleaned[:max_len]


@dataclass
class _Candidate:
    client_id: str
    peer_ip: str
    claim_secret: str
    label: str
    room_id: str | None
    created_at: float
    accepted: bool = False
    accepted_at: float | None = None


@dataclass(frozen=True)
class PairOutcome:
    """Result of a ``POST /pair``. ``http`` is the status the handler returns; ``claim_secret`` rides a
    ``202``; ``auth_token`` rides the ``200``. ``error`` is the machine-readable code for non-2xx."""
    status: str          # pending | accepted | issued | no_window | ip_conflict | bad_secret | bad_client_id
    http: int
    claim_secret: str | None = None
    auth_token: str | None = None
    error: str | None = None


class PairingState:
    """The pairing state machine: one operator WINDOW + a set of per-``client_id`` CLAIMs.

    ``secret_factory`` mints the server-side ``claim_secret`` (default: installkit's 128-bit
    ``generate_token``); ``clock`` is the monotonic time source (injectable for TTL tests).
    """

    def __init__(self, *, window_ttl: float = DEFAULT_WINDOW_TTL, claim_ttl: float = DEFAULT_CLAIM_TTL,
                 secret_factory=generate_token, clock=_time.monotonic) -> None:
        self._window_ttl = float(window_ttl)
        self._claim_ttl = float(claim_ttl)
        self._secret_factory = secret_factory
        self._clock = clock
        self._window_until: float = 0.0
        self._candidates: dict[str, _Candidate] = {}

    # ---- operator (admin) side -------------------------------------------------------------------
    def open_window(self) -> float:
        """Open (or extend) the pairing window. Returns seconds until it closes."""
        now = self._clock()
        self._prune(now)
        self._window_until = now + self._window_ttl
        return self._window_ttl

    def window_open(self) -> bool:
        return self._clock() < self._window_until

    def candidates(self) -> list[dict]:
        """Live candidates for the operator display (label already sanitised; peer_ip is authoritative)."""
        now = self._clock()
        self._prune(now)
        return [
            {"client_id": c.client_id, "peer_ip": c.peer_ip, "label": c.label,
             "room_id": c.room_id, "age_s": round(now - c.created_at, 1), "accepted": c.accepted}
            for c in self._candidates.values()
        ]

    def accept(self, client_id: str) -> bool:
        """Operator accepts ONE candidate: it becomes retrievable (claim TTL starts now), the window
        closes to new candidates, and every OTHER candidate is dropped — so a lingering attacker
        candidate cannot survive the accept. Returns False if there is no such live candidate."""
        now = self._clock()
        self._prune(now)
        c = self._candidates.get(client_id)
        if c is None:
            return False
        c.accepted = True
        c.accepted_at = now
        self._window_until = 0.0                 # window closes on accept — no new candidates
        self._candidates = {client_id: c}        # drop all others
        return True

    # ---- client (satellite) side: the CLAIM state machine ----------------------------------------
    def pair(self, *, client_id: str, claim_secret: str | None, peer_ip: str,
             label: object, room_id: str | None, auth_token: str) -> PairOutcome:
        """One ``POST /pair`` step. ``auth_token`` is the brain's bearer secret, supplied by the handler
        and returned only on a valid post-accept retrieval — the state machine never stores it."""
        now = self._clock()
        self._prune(now)
        cid = (client_id or "").strip()
        if len(cid) < MIN_CLIENT_ID_LEN:
            return PairOutcome("bad_client_id", 400,
                               error=f"client_id must be >={MIN_CLIENT_ID_LEN} chars (>=128-bit random)")
        pip = (peer_ip or "").strip()

        existing = self._candidates.get(cid)
        if existing is not None and existing.peer_ip != pip:
            # Different source IP presenting an existing client_id: the first peer owns it. Never merge —
            # this is the off-path/collision guard (a 2^-128 event given the entropy floor; belt over
            # suspenders). The legitimate holder keeps its candidate; the imposter is refused.
            return PairOutcome("ip_conflict", 409, error="client_id_registered_from_different_peer")

        if existing is None:
            if now >= self._window_until:
                return PairOutcome("no_window", 409, error="no_pairing_window_open")
            secret = self._secret_factory()
            self._candidates[cid] = _Candidate(
                client_id=cid, peer_ip=pip, claim_secret=secret,
                label=sanitize_label(label), room_id=room_id, created_at=now)
            return PairOutcome("pending", 202, claim_secret=secret)

        # Existing candidate, same peer.
        if not existing.accepted:
            # Idempotent: re-return the SAME secret (recovers a dropped first 202), whether or not the
            # client presented one. Still pending → keep polling.
            return PairOutcome("pending", 202, claim_secret=existing.claim_secret)

        # Accepted → retrieval phase. `_prune` (run at entry) has already dropped this candidate if its
        # claim TTL elapsed — so a timed-out claim reads back as "no candidate + closed window" =
        # `no_pairing_window_open` above (a 409 the client recovers from by re-pairing), and by here the
        # accepted claim is guaranteed live.
        presented = (claim_secret or "").strip()
        if presented:
            if hmac.compare_digest(presented, existing.claim_secret):
                # Replay-until-TTL for the secret holder: a dropped 200 recovers on retry.
                return PairOutcome("issued", 200, auth_token=auth_token)
            return PairOutcome("bad_secret", 403, error="bad_claim_secret")
        # Secretless re-POST in the accepted phase: re-return the secret so a client whose FIRST 202
        # dropped (and who was then accepted before recovering it) can still retrieve. Safe: this
        # requires the >=128-bit client_id AND the pinned source IP.
        return PairOutcome("accepted", 202, claim_secret=existing.claim_secret)

    # ---- internal --------------------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        """Drop expired candidates: an accepted claim past its TTL, or a still-pending candidate whose
        admitting window has closed (a pending candidate lives only as long as that window)."""
        drop = [
            cid for cid, c in self._candidates.items()
            if (c.accepted and (c.accepted_at is None or now > c.accepted_at + self._claim_ttl))
            or (not c.accepted and now >= self._window_until)
        ]
        for cid in drop:
            del self._candidates[cid]
