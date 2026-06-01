"""Per-session voice state: confirm round-trips, undo stack, Tier-3 arming."""
from __future__ import annotations
import asyncio
import time
from typing import Any


class VoiceSession:
    def __init__(self, session_id: str, ctx: Any, audit_path: Any = None):
        self.session_id = session_id
        self.ctx = ctx
        self.audit_path = audit_path
        # Pending confirmation round-trips: confirm id -> Future[(approved, passphrase)]
        self.pending: dict[str, asyncio.Future] = {}
        # Serializes overlapping confirm round-trips (parallel gated tools).
        self.lock = asyncio.Lock()
        # The in-flight `drive` task for the current turn (for /cancel).
        self.active_task: asyncio.Task | None = None
        # Undo stack for voice-made writes: (path, prior_bytes_or_None_if_new).
        self.undo_stack: list[tuple[str, bytes | None]] = []
        # Tier-3 arming windows: tool_family -> expires_at (epoch seconds).
        self.armed: dict[str, float] = {}

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
