"""TmiManager — the recall facade for the tiered-memory layer (P1: READ path only).

Writing and reconciliation (consolidate/prune/escalate) land in later phases; here we only assemble
what a turn recalls, blending the tiers. The manager is cheap to construct per turn (mirrors the
existing per-turn PersonaManager/MemoryManager construction) and fully defensive — a memory failure
must never perturb a turn. Inert unless config.tmi.enabled.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Per-tier injection caps (chars). Tier 0 matches the persona INDEX budget; Tier 1 matches the
# legacy memory-brief budget — so per-turn prompt cost stays flat as the stores grow.
_TIER0_CAP = 1200
_TIER1_CAP = 1500


class TmiManager:
    def __init__(self, ctx: AgentContext) -> None:
        self._ctx = ctx
        rid = getattr(ctx, "room_id", None)
        self._room_id = rid.strip() if isinstance(rid, str) and rid.strip() else None

    def tier0_brief(self, limit: int = _TIER0_CAP) -> str:
        """Tier 0 — the shared cross-room identity. Today that IS the persona store (persona is
        subsumed into Tier 0); P1 delegates to it rather than forking a second identity store."""
        try:
            from gabagent.persona.manager import PersonaManager
            return PersonaManager().brief(limit=limit)
        except Exception:
            return ""

    def tier1_brief(self, limit: int = _TIER1_CAP) -> str:
        """Tier 1 — this room's durable notes. Reads the per-room store, with READ-THROUGH to the
        legacy cwd memory until writes are re-keyed to the room (a later phase). Empty ⇒ ''."""
        text = self._room_memory()
        if not text:
            text = self._legacy_cwd_memory()  # read-through, so existing notes still surface
        if not text:
            return ""
        if len(text) > limit:
            text = "…" + text[-limit:]
        return text

    # -- internals ---------------------------------------------------------
    def _room_memory(self) -> str:
        try:
            from gabagent.config.paths import room_dir
            p = room_dir(self._room_id) / "memory.md"
            if p.exists():
                return p.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return ""

    def _legacy_cwd_memory(self) -> str:
        try:
            from gabagent.session.memory import MemoryManager
            return MemoryManager(self._ctx.cwd).load().strip()
        except Exception:
            return ""
