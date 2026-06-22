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
        """Tier 0 — the shared cross-room identity: persona identity (priority, never truncated) blended
        with the escalated cross-room facts, under one char budget so per-turn cost stays flat (this cap,
        not the 50-fact store cap, is what protects a local-model brain). Escalated facts appear only
        when Tier-0 escalation is enabled and the store is non-empty; otherwise this is persona-only,
        exactly as P1/P2 behave."""
        try:
            from gabagent.persona.manager import PersonaManager
            persona = PersonaManager().brief(limit=limit)
        except Exception:
            persona = ""
        facts = self._tier0_facts_block(max(0, limit - len(persona)))
        return "\n\n".join(p for p in (persona, facts) if p)

    def _tier0_facts_block(self, budget: int) -> str:
        cfg = getattr(self._ctx, "config", None)
        tmi = getattr(cfg, "tmi", None)
        if budget <= 0 or not (tmi and getattr(tmi, "enabled", False)
                               and getattr(tmi, "tier0_escalation_enabled", False)):
            return ""
        try:
            import time
            from gabagent.config.paths import tier0_dir
            from gabagent.tmi.facts import FactStore
            from gabagent.tmi import weights
            facts = FactStore(tier0_dir() / "facts.jsonl").load()
        except Exception:
            return ""
        alert = self._consume_cap_alert(tier0_dir())
        if not facts:
            return alert
        now = time.time()
        facts.sort(key=lambda f: weights.score(f, now, 30), reverse=True)
        lines = "\n".join(f"- {f.text}" for f in facts)
        if len(lines) > budget:
            lines = lines[:budget].rstrip() + "…"
        block = "Shared across every room (what's true of you everywhere):\n" + lines
        return f"{block}\n\n{alert}" if alert else block

    @staticmethod
    def _consume_cap_alert(d) -> str:
        """One-shot: if Tier 0 deadlocked (full of protected facts), tell Aria to mention it once, then
        clear the flag so she says it only on the next turn."""
        p = d / "cap_alert.json"
        try:
            if not p.exists():
                return ""
            p.unlink()
            return ("NOTE: your shared memory is full of facts the user pinned, so nothing new can be "
                    "saved there. Mention this ONCE, naturally, and ask which to let go of.")
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
