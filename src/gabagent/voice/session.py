"""Per-session voice state: confirm round-trips, undo stack, Tier-3 arming, project attach."""
from __future__ import annotations
import asyncio
import time
from typing import Any

_UNSET = object()  # sentinel: no project change staged (distinct from a staged detach, which is None)


class VoiceSession:
    def __init__(self, session_id: str, ctx: Any, audit_path: Any = None):
        self.session_id = session_id
        self.ctx = ctx
        self.audit_path = audit_path
        # Multi-room foundation (STT-offload seam). `room_id` is the DURABLE per-room routing key
        # (stable per physical room/device), distinct from `session_id` which is EPHEMERAL (per brain
        # process / per voice-client init). Set on /attach, refreshed from any payload's optional
        # room_id. Ignored for routing today — the brain is still single-conversation (one ctx) — but
        # wired here so materializing a per-room ctx later isn't a retrofit. `capabilities` is the
        # client's /attach advertisement ({wake,vad,stt,tts}); logged now, drives placement later.
        self.room_id: str | None = None
        self.capabilities: dict = {}
        # Pending confirmation round-trips: confirm id -> Future[(approved, passphrase)]
        self.pending: dict[str, asyncio.Future] = {}
        # Serializes overlapping confirm round-trips (parallel gated tools).
        self.lock = asyncio.Lock()
        # The persistent turn task. It outlives a single HTTP request: it stays
        # alive (suspended at a confirm) between /respond and /confirm, and is
        # cancelled by /cancel. Its events flow through `queue`, drained by
        # whichever request is currently streaming.
        self.turn_task: asyncio.Task | None = None
        self.queue: asyncio.Queue | None = None
        # Undo stack for voice-made writes: (path, prior_bytes_or_None_if_new).
        self.undo_stack: list[tuple[str, bytes | None]] = []
        # Tier-3 arming windows: tool_family -> expires_at (epoch seconds).
        self.armed: dict[str, float] = {}
        # Last turn-level failure, for the "what went wrong?" query.
        self.last_error: str | None = None
        self.last_error_time: float | None = None
        # Countdown timers (G2): id -> Timer. Fired by voice/timers.py's async ticker.
        # Session-scoped (lost on a brain restart in v1); _timer_n is the per-session id counter.
        self.timers: dict = {}
        self._timer_n: int = 0
        # Voice project attach (Part B). `active_project` is the currently-attached project root (or
        # None = ambient). `base_cwd` is the pre-attach cwd, saved on first attach so "leave" restores
        # it. A change is STAGED by the attach/leave tool and applied by `apply_pending` at the NEXT
        # turn boundary — never mid-round, so a same-round write_file can't race on ctx.cwd.
        # Session-scoped (lost on a brain restart, like timers).
        self.active_project: Any = None   # Path | None
        self.base_cwd: Any = None         # Path | None
        self._pending_project: Any = _UNSET  # _UNSET = nothing staged; Path = attach; None = detach

    # -- project attach (Part B) --------------------------------------------
    def stage_project(self, target: Any) -> None:
        """Stage an attach (target=Path) or detach (target=None) to apply at the next turn boundary."""
        self._pending_project = target

    def apply_pending_project(self, ctx: Any) -> Any:
        """Apply any staged attach/detach to ctx.cwd. Called once at the top of a turn, before the
        round loop, so it never races a same-round tool. Returns the new active_project (or None)."""
        if self._pending_project is _UNSET:
            return self.active_project
        target = self._pending_project
        self._pending_project = _UNSET
        if self.base_cwd is None:
            self.base_cwd = ctx.cwd
        if target is None:
            ctx.cwd = self.base_cwd
            self.active_project = None
        else:
            ctx.cwd = target
            self.active_project = target
        return self.active_project

    def set_error(self, msg: str) -> None:
        self.last_error = msg
        self.last_error_time = time.time()

    # -- confirm round-trip --------------------------------------------------
    def new_confirm(self, cid: str) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        return fut

    def resolve(self, cid: str, approved: bool, passphrase: str | None = None) -> bool:
        fut = self.pending.pop(cid, None)
        if fut is not None and not fut.done():
            fut.set_result((approved, passphrase))
            return True
        return False

    def clear_pending(self, approved: bool = False) -> None:
        for cid, fut in list(self.pending.items()):
            if not fut.done():
                fut.set_result((approved, None))
        self.pending.clear()

    # -- Tier-3 arming -------------------------------------------------------
    def arm(self, family: str, seconds: int) -> None:
        if seconds > 0:
            self.armed[family] = time.time() + seconds

    def is_armed(self, family: str) -> bool:
        exp = self.armed.get(family)
        if exp is None:
            return False
        if time.time() >= exp:
            self.armed.pop(family, None)
            return False
        return True

    def disarm_all(self) -> None:
        self.armed.clear()

    # -- undo ----------------------------------------------------------------
    def push_undo(self, path: str, prior: bytes | None) -> None:
        self.undo_stack.append((path, prior))

    def pop_undo(self) -> tuple[str, bytes | None] | None:
        return self.undo_stack.pop() if self.undo_stack else None
