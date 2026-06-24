"""Disk-backed, liveness-leased queue of deferred spoken announcements — the steady, SLEEP-INDEPENDENT
brain→voice proactive channel (phase 2). One JSON file per pending item.

This is the channel that carries bot-initiated speech (builder results, timer rings, future nudges) out
to the voice side, which drains it on a fixed-cadence `GET /builder/poll` REGARDLESS of media/sleep — the
`/media/state` duck poll is dead when asleep (the voice wake-gate short-circuits before reaching the brain),
so proactive delivery cannot ride it. SHARED infra: the builder runner and the timer ticker both enqueue
here; the poll endpoint drains it.

Delivery is two-phase and keyed on POLLER LIVENESS, not on a speak-deadline. The voice floor can stay
closed for an UNBOUNDED time (Aria sleeps until a wake word — `brain_llm_service.is_floor_free` is False
while `_sleeping`, cleared only by a wake transcript), so an item must NOT expire just because it hasn't
been spoken yet. An item is claimed by the first eligible poller, held while that poller keeps polling
(each poll renews the claim = proof of life), and reverts to `ready` only if the claimer goes dark for
`lease_secs` (≈ a few missed poll cycles ⇒ truly gone/shut down). The poller acks by `job_id` AFTER
speaking, which finalizes (removes) the item. Single-holder is guaranteed by an atomic claim under flock.

Routing is DEVICE-FREE with an originating-first preference (Rob 2026-06-23): an item prefers the session
that produced it (a timer's owning session); any other poller may take it only after the preferred device
fails to claim it within `lease_secs` (it walked off / shut down). On a gone-revert the preference is
cleared (the preferred device proved absent) so any device gets it next. Builder results have no owning
session → `preferred_session=None` ⇒ free-for-all, immediately deliverable to any device. Presence-aware
routing (serve the device Rob is physically AT, via last-VAD/last-wake) is a roadmapped phase-2.x refinement.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from gabagent.config.paths import data_dir

# Default liveness gone-timeout / originating-first fallback grace. Must comfortably exceed a few of the
# voice side's poll cycles (~1.5s) so a still-alive poller is never judged gone. Safe universal default;
# overridable per-install via config (voice_announce_lease_secs) — passed in by the poll endpoint.
DEFAULT_LEASE_SECS = 8.0
_LOCK_TIMEOUT = 5.0


def announce_dir() -> Path:
    p = data_dir() / "announce"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _item_path(item_id: str) -> Path:
    return announce_dir() / f"{item_id}.json"


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# -- producers (builder runner, timer ticker) — write a new ready item -------

def enqueue(
    text: str,
    job_id: str | None = None,
    source: str = "",
    preferred_session: str | None = None,
    now: float | None = None,
) -> dict | None:
    """Enqueue a deferred announcement. `text` is the FULLY-PHRASED spoken line (the voice side speaks it
    verbatim). `job_id` is the delivery key the poller acks on (defaults to a fresh unique id). Empty text
    is dropped. Returns the item, or None if dropped. Lock-free: a unique filename + atomic write can't
    collide with a concurrent poll/ack (which only ever touch existing files under the poll lock)."""
    text = (text or "").strip()
    if not text:
        return None
    now = time.time() if now is None else now
    item_id = job_id or f"ann-{int(now)}-{uuid.uuid4().hex[:8]}"
    item: dict[str, Any] = {
        "job_id": item_id,
        "text": text,
        "source": source,                       # "builder" | "timer" | … (debugging; not on the wire)
        "status": "ready",                      # ready -> delivering -> (removed on ack)
        "preferred_session": preferred_session, # originating-first; None ⇒ free-for-all
        "claimed_by": None,
        "claim_renewed_ts": None,
        "ready_since": now,
        "created_ts": now,
    }
    _atomic_write(_item_path(item_id), item)
    return item


# -- consumer (the /builder/poll endpoint) -----------------------------------

def _lock():
    """One store-wide lock for the read-modify-write of poll()/ack() (many files mutate per poll, so a
    single lock is simpler and correct than per-file locks). Producers don't take it — they only add new,
    uniquely-named files. Returns an open fd or None if the lock can't be acquired in time."""
    lock_path = announce_dir() / ".poll.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        return None
    deadline = time.time() + _LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if time.time() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.02)


def _unlock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def poll(session_id: str, now: float | None = None, lease_secs: float = DEFAULT_LEASE_SECS) -> list[dict]:
    """A poll from `session_id`: renew this session's live claims, revert any genuinely-gone claims, and
    assign newly-eligible ready items to this session. Returns the items NEWLY assigned this poll as
    `[{job_id, text}]` (the voice side speaks them, then acks by job_id). An item already delivering to
    this session is NOT re-returned — it was handed over on a prior poll; this poll just renews its
    liveness so it isn't reverted while the floor stays closed."""
    now = time.time() if now is None else now
    session_id = session_id or ""
    fd = _lock()
    if fd is None:
        return []  # contended → caller polls again shortly; convergent
    assigned: list[dict] = []
    try:
        for path in sorted(announce_dir().glob("*.json"), key=lambda p: p.name):
            item = _read(path)
            if item is None:
                continue
            dirty = False
            if item["status"] == "delivering":
                if item.get("claimed_by") == session_id:
                    item["claim_renewed_ts"] = now           # proof of life — hold the claim
                    dirty = True
                else:
                    renewed = item.get("claim_renewed_ts")
                    if renewed is None or now - renewed > lease_secs:
                        item.update(status="ready", claimed_by=None, claim_renewed_ts=None,
                                    preferred_session=None, ready_since=now)  # holder gone → free it
                        dirty = True
            if item["status"] == "ready":
                pref = item.get("preferred_session")
                ready_since = item.get("ready_since")
                ready_since = now if ready_since is None else ready_since  # NOT `or` — 0.0 is a valid ts
                eligible = (
                    pref in (None, "", session_id)
                    or now - ready_since > lease_secs  # preferred device never grabbed it within the grace
                )
                if eligible:
                    item.update(status="delivering", claimed_by=session_id, claim_renewed_ts=now)
                    assigned.append({"job_id": item["job_id"], "text": item["text"]})
                    dirty = True
            if dirty:
                _atomic_write(path, item)
    finally:
        _unlock(fd)
    return assigned


def ack(job_id: str) -> bool:
    """Finalize a spoken item — remove it. Idempotent (a re-ack of an already-gone item is a no-op)."""
    if not job_id:
        return False
    fd = _lock()
    if fd is None:
        return False
    try:
        path = _item_path(job_id)
        if path.exists():
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False
        return False
    finally:
        _unlock(fd)


def pending(session_id: str | None = None) -> list[dict]:
    """All pending items (debug / explicit-pull), newest-last. Optionally filter to those a given session
    owns or that are unclaimed."""
    out = []
    for path in announce_dir().glob("*.json"):
        item = _read(path)
        if item is None:
            continue
        if session_id is not None:
            pref = item.get("preferred_session")
            if pref not in (None, "", session_id) and item.get("claimed_by") not in (None, session_id):
                continue
        out.append(item)
    out.sort(key=lambda j: j.get("created_ts", 0))
    return out
