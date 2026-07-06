"""Cross-room wake arbiter — the brain-side "first-to-hear" referee that decides which single device owns
one "Hey Aria" when two rooms hear it (the door-open hall-leak double-answer, issue #66). Stage 2 of the
two-stage fix; Stage 1 (per-room wake-threshold zoning, voice-side) ships first and this is the fallback
flipped on only if the gross acoustic separation still doubles. OFF by default (`voice_wake_arbiter_enabled`).

WHY THIS SHAPE (converged GA↔VAC design, 2026-07-06):
- HOME = an EM-disk-local flock'd window file, mirroring `announce_store`'s single-holder discipline. The two
  colliding rooms are two brain PROCESSES on EM (host-loopback + Pi-facing), so they share EM's disk and its
  single wall clock — a co-located referee is real. A third brain on another host (the ZeroTier laptop) can't
  touch EM's disk → it never joins a window → any solo/remote install is byte-identical to today, for free.
  Do NOT move this to a LAN broadcast/multicast home that a cross-host brain would half-join.
- SIGNAL = windowed EARLIEST-*NORMALIZED*-RECEIPT, not first-wins and not loudest (the reSpeaker AEC/AGC
  inverts level, so "loudest" is backwards for proximity). Each claim's server-side EM arrival time minus that
  device's CALIBRATED detector latency (wake models differ ~0.5s vs ~2.3s; un-normalized arrival would let a
  fast-detector far room beat a slow-detector near room). Server-receipt time, never the client `ts` — two
  client clocks would reintroduce the NTP skew the one-EM-clock design exists to avoid.
- NEVER-ZERO = exactly one winner per window, and if that winner never actually answers (its /respond never
  marks the window), the stood-down room un-stands-down after a grace. Worst case is today's double answer,
  never a zero answer. The stood-down room learns this ONLY by asking the referee (`check_fallback`), never by
  observing the winner directly (that cross-device observation is the "speaking beacon" the design killed).
- media-exempt: a device with local media playing wins close calls so the other room grabbing the turn can't
  interrupt its playback; still a single winner (no double-answer).

The one honest worse-than-today case: two GENUINELY DISTINCT near-simultaneous "Aria"s (bedroom + living room
within the window) get merged → the loser's DIFFERENT real command is silenced. Unfixable pre-STT (no transcript
yet to tell one-voice-two-mics from two-voices); only shrinkable by keeping the window as tight as calibration
allows. Rob weighed and accepted this trade.

All store functions are synchronous and flock-guarded (like announce_store); the short grace WAIT and the
fallback wait live in the /prewarm endpoint (async), never under the lock.
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

_LOCK_TIMEOUT = 5.0
# A media-playing claim is made decisively earliest so the other room can't grab the turn out from under its
# playback; still a single winner (min effective time). Large enough to dominate any within-window delta.
_MEDIA_BIAS = 1.0e6


def arbiter_dir() -> Path:
    p = data_dir() / "wake_arbiter"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _win_path(window_id: str) -> Path:
    return arbiter_dir() / f"{window_id}.json"


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


def _lock():
    """Store-wide lock for the read-modify-write of a window (mirrors announce_store). Returns an open fd or
    None if it can't be acquired in time (contended → caller retries; convergent)."""
    lock_path = arbiter_dir() / ".wake.lock"
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
            time.sleep(0.005)


def _unlock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _gc(now: float, keep_secs: float) -> None:
    """Drop windows older than keep_secs past their close (must be called under lock)."""
    for path in arbiter_dir().glob("win-*.json"):
        w = _read(path)
        if w is None:
            continue
        anchor = w.get("closed_ts") or w.get("decide_at") or w.get("opened_ts") or 0.0
        if now - anchor > keep_secs:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def claim(
    room: str,
    detector_latency_ms: float = 0.0,
    media_playing: bool = False,
    window_secs: float = 0.25,
    now: float | None = None,
) -> dict:
    """Register a wake claim for `room`. Joins the current OPEN window (one still accepting peers, i.e.
    `now < decide_at`) or opens a fresh one. Idempotent per room per window — a re-fire keeps the earliest
    normalized time. Returns `{window_id, claim_id, decide_at}`; the endpoint waits until `decide_at` then
    calls `resolve()`. `detector_latency_ms` is the device's calibrated wake-model latency, subtracted from
    the server-receipt time to normalize near-vs-far. `now` = server EM receipt time (wall clock)."""
    now = time.time() if now is None else now
    room = room or "default"
    normalized_ts = now - max(0.0, float(detector_latency_ms)) / 1000.0
    fd = _lock()
    if fd is None:
        # Can't coordinate → fail OPEN (proceed) so a contended lock never silences a room.
        return {"window_id": "", "claim_id": "", "decide_at": now}
    try:
        _gc(now, keep_secs=8.0)
        # Newest still-open window, if any.
        win = None
        win_path = None
        for path in sorted(arbiter_dir().glob("win-*.json"), reverse=True):
            w = _read(path)
            if w is None:
                continue
            if not w.get("decided") and now < w.get("decide_at", 0.0):
                win, win_path = w, path
                break
        if win is None:
            window_id = f"win-{int(now * 1000)}-{uuid.uuid4().hex[:6]}"
            win = {
                "window_id": window_id,
                "opened_ts": now,
                "decide_at": now + max(0.0, float(window_secs)),
                "claims": {},
                "decided": False,
                "winner_room": None,
                "committed_ts": None,
                "answered_ts": None,
                "closed_ts": None,
            }
            win_path = _win_path(window_id)
        claims = win["claims"]
        prior = claims.get(room)
        claim_id = (prior or {}).get("claim_id") or f"c-{uuid.uuid4().hex[:8]}"
        # Keep the earliest normalized time if this room already claimed (e.g. a double-fire).
        if prior is None or normalized_ts < prior.get("normalized_ts", float("inf")):
            claims[room] = {
                "room": room,
                "claim_id": claim_id,
                "normalized_ts": normalized_ts,
                "receipt_ts": now,
                "media_playing": bool(media_playing),
            }
        elif media_playing and not prior.get("media_playing"):
            prior["media_playing"] = True
        _atomic_write(win_path, win)
        return {"window_id": win["window_id"], "claim_id": claim_id, "decide_at": win["decide_at"]}
    finally:
        _unlock(fd)


def _effective(claim: dict) -> float:
    ts = claim.get("normalized_ts", float("inf"))
    return ts - _MEDIA_BIAS if claim.get("media_playing") else ts


def resolve(window_id: str, room: str, now: float | None = None) -> dict:
    """Return `room`'s verdict for a (now-closed) window, computing and persisting the winner on first call
    past `decide_at`. `{verdict: 'proceed'|'stand_down'|'pending', winner_room, window_id}`. 'pending' means
    the window hasn't closed yet (caller waited too little); 'proceed' if the window/lock is gone (fail-open).
    Winner = the claim with the earliest effective time (normalized receipt, media-playing biased earliest)."""
    now = time.time() if now is None else now
    if not window_id:
        return {"verdict": "proceed", "winner_room": room, "window_id": window_id}
    fd = _lock()
    if fd is None:
        return {"verdict": "proceed", "winner_room": room, "window_id": window_id}
    try:
        path = _win_path(window_id)
        win = _read(path)
        if win is None:
            return {"verdict": "proceed", "winner_room": room, "window_id": window_id}
        if not win.get("decided"):
            if now < win.get("decide_at", 0.0):
                return {"verdict": "pending", "winner_room": None, "window_id": window_id}
            claims = win.get("claims") or {}
            if claims:
                winner = min(claims.values(), key=_effective)["room"]
            else:
                winner = room
            win["decided"] = True
            win["winner_room"] = winner
            win["closed_ts"] = now
            _atomic_write(path, win)
        winner = win.get("winner_room")
        verdict = "proceed" if winner == room else "stand_down"
        return {"verdict": verdict, "winner_room": winner, "window_id": window_id}
    finally:
        _unlock(fd)


def mark_answered(room: str, now: float | None = None, grace_secs: float = 6.0) -> bool:
    """Called from /respond when a real turn starts: mark the most-recent decided window this room WON as
    answered, so a stood-down peer's fallback stays down. Only recent windows (within grace) are eligible —
    an old win never suppresses a genuinely new, uncontested turn. Returns True if a window was marked."""
    now = time.time() if now is None else now
    room = room or "default"
    fd = _lock()
    if fd is None:
        return False
    try:
        best_path = None
        best_ts = -1.0
        for path in arbiter_dir().glob("win-*.json"):
            w = _read(path)
            if w is None or not w.get("decided") or w.get("winner_room") != room:
                continue
            if w.get("answered_ts") is not None:
                continue
            closed = w.get("closed_ts") or 0.0
            if now - closed > grace_secs:
                continue
            if closed > best_ts:
                best_ts, best_path = closed, path
        if best_path is None:
            return False
        w = _read(best_path)
        if w is None:
            return False
        w["answered_ts"] = now
        _atomic_write(best_path, w)
        return True
    finally:
        _unlock(fd)


def mark_committed(window_id: str, room: str, now: float | None = None) -> bool:
    """The WINNER stamps liveness the instant it ACCEPTS its `proceed` verdict — BEFORE STT, not after. This
    is what decouples the never-zero fallback from turn duration: a real turn doesn't reach /respond for 6-26s
    (measured), far longer than a stood-down peer will wait, so keying the fallback on the post-STT `answered`
    mark alone would double-answer every live-but-still-transcribing winner. The commit lands in ~ms, so the
    peer's probe sees "the winner started" long before the turn finishes. Idempotent refresh (a heartbeat may
    call it repeatedly to also cover mid-turn death when liveness_secs>0). Only the decided window's winner may
    commit. Returns True if stamped."""
    now = time.time() if now is None else now
    room = room or "default"
    if not window_id:
        return False
    fd = _lock()
    if fd is None:
        return False
    try:
        path = _win_path(window_id)
        win = _read(path)
        if win is None or not win.get("decided") or win.get("winner_room") != room:
            return False
        win["committed_ts"] = now
        _atomic_write(path, win)
        return True
    finally:
        _unlock(fd)


def check_fallback(window_id: str, room: str, now: float | None = None, liveness_secs: float = 0.0) -> dict:
    """A stood-down room asks the referee whether the winner actually took the turn. `{verdict:
    'answered'|'proceed'}` — 'answered' ⇒ stay down; 'proceed' ⇒ un-stand-down and handle it (never-zero).
    The winner is "taking it" if it ANSWERED (terminal /respond mark) OR it COMMITTED (accepted proceed). With
    liveness_secs<=0 this is PRESENCE-based: any commit keeps the peer down — a winner that started then died
    mid-turn is a single-device failure, out of arbiter scope (the far room answering a stale utterance while
    the user stands in the winner's room is worse than silence). With liveness_secs>0 the commit must be FRESH
    within that grace (heartbeat mode) — so a winner that goes silent past the grace releases the peer. 'proceed'
    when the winner never committed (never started). Never observes the winner directly — the referee, co-located
    with the winner's /prewarm+/respond, does. Fail-open ('proceed') if the window/lock is gone."""
    now = time.time() if now is None else now
    if not window_id:
        return {"verdict": "proceed"}
    fd = _lock()
    if fd is None:
        return {"verdict": "proceed"}
    try:
        win = _read(_win_path(window_id))
        if win is None:
            return {"verdict": "proceed"}
        if win.get("answered_ts") is not None:
            return {"verdict": "answered"}
        c = win.get("committed_ts")
        if c is not None and (liveness_secs <= 0 or now - c <= liveness_secs):
            return {"verdict": "answered"}
        return {"verdict": "proceed"}
    finally:
        _unlock(fd)
