"""TmiEscalator — promote high-signal per-room (Tier 1) facts into the shared cross-room Tier 0.

Runs on the detached shutdown child, AFTER the per-room reconcile, gated behind
config.tmi.tier0_escalation_enabled. Pipeline:

  nominate (pure over Tier-1 metadata: privacy → explicit OR habit)
    → judge (one batched top-rung LLM call: "Aria-everywhere vs this-room?", OUTSIDE the lock)
    → merge under a flock on tier0/.lock (dedupe/bump, cross-room boost)
    → apply the adaptive pressure band ONCE (decay → prune → admit/evict to the hard cap)
    → write Tier 0 atomically; flag the all-protected-at-cap deadlock for a live turn to voice.

Defensive by contract: never raises, never blocks shutdown. If the lock can't be taken in time, the
escalation is SKIPPED this cycle — candidates stay in Tier 1 and are re-nominated next shutdown
(convergent, because the gate is a pure function of persisted Tier-1 metadata).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gabagent.tmi.facts import Fact, FactStore, normalize
from gabagent.tmi import weights

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_LOCK_TIMEOUT = 2.0   # seconds to wait for the Tier-0 lock before skipping this cycle
_JUDGE_SYSTEM = (
    "You are the gatekeeper for Aria's SHARED memory — facts that should be true of her in EVERY room "
    "she serves, not just one. You are given candidate facts nominated from one room. Return ONLY the "
    "ones that genuinely belong to Aria everywhere (durable user identity, preferences, relationships, "
    "standing instructions). REJECT anything room-specific, transient, or that reads like speech-to-text "
    "noise. Be conservative — shared memory is precious and tiny.\n\n"
    "Output EXACTLY:\n<KEEP>\n(each accepted fact verbatim on its own line, or empty if none)\n</KEEP>"
)


class TmiEscalator:
    def __init__(self, room_id: str | None) -> None:
        self._room_id = (room_id or None) if (room_id or "").strip() else None

    async def escalate_from_ctx(self, ctx: "AgentContext", now: float | None = None) -> int:
        """Run the full escalation pass for this room. Returns the number of facts written to Tier 0
        (0 if disabled, nothing qualified, or the lock was skipped). Never raises."""
        try:
            cfg = getattr(ctx, "config", None)
            tmi = getattr(cfg, "tmi", None)
            if tmi is None or not getattr(tmi, "enabled", False) or not getattr(tmi, "tier0_escalation_enabled", False):
                return 0
            if (self._room_id or "default") in (getattr(tmi, "tier0_exclude_rooms", None) or []):
                return 0  # privacy: this room never feeds shared memory
        except Exception:
            return 0

        ts = now if now is not None else time.time()
        try:
            from gabagent.config.paths import room_dir
            room_facts = FactStore(room_dir(self._room_id) / "facts.jsonl").load()
        except Exception:
            return 0

        nominees = self._nominate(room_facts, tmi, ts)
        if not nominees:
            return 0
        confirmed = await self._judge(ctx, nominees)  # top-rung LLM, OUTSIDE the lock
        if not confirmed:
            return 0
        return self._commit(confirmed, tmi, ts)

    # -- nomination (pure) -------------------------------------------------
    def _nominate(self, facts: list[Fact], tmi, ts: float) -> list[Fact]:
        out = []
        for f in facts:
            if f.explicit or self._is_habit(f, tmi, ts):
                out.append(f)
        return out

    @staticmethod
    def _is_habit(f: Fact, tmi, ts: float) -> bool:
        """Persistence, not a spike: >= habit_count re-observations across >= 2 distinct weeks AND a
        >= habit_window_days span. The conjunction is load-bearing — hits alone counts same-day
        reconciles, which the distinct-week test rejects."""
        count = getattr(tmi, "habit_count", 3)
        window = getattr(tmi, "habit_window_days", 14) * 86400
        return (f.hits >= count
                and len(set(f.seen_weeks)) >= 2
                and (f.last_seen - f.first_seen) >= window)

    # -- judgment gate (LLM, outside the lock) -----------------------------
    async def _judge(self, ctx, nominees: list[Fact]) -> list[Fact]:
        from gabagent.api.models import ChatMessage
        from gabagent.persona.manager import PersonaManager, _between
        listing = "\n".join(f"- {f.text}" for f in nominees)
        prompt = [
            ChatMessage(role="system", content=_JUDGE_SYSTEM),
            ChatMessage(role="user", content=f"Candidate facts from room '{self._room_id or 'default'}':\n{listing}"),
        ]
        try:
            client, model, effort = PersonaManager._reflection_client(ctx)
            out = await client.complete_simple(prompt, model=model, effort=effort)
        except Exception:
            return []  # judge unavailable → skip; nominees persist and retry next cycle
        kept = {normalize(ln) for ln in _between(out, "KEEP").splitlines() if normalize(ln)}
        return [f for f in nominees if f.key in kept]

    # -- merge + banded cap under the Tier-0 lock --------------------------
    def _commit(self, confirmed: list[Fact], tmi, ts: float) -> int:
        import fcntl
        import os
        from gabagent.config.paths import tier0_dir

        d = tier0_dir()
        lock_path = d / ".lock"  # distinct inode — never the data file os.replace swaps
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except Exception:
            return 0
        try:
            if not self._acquire(fcntl, fd):
                return 0  # contended → skip; candidates re-nominated next cycle (convergent)
            store = FactStore(d / "facts.jsonl")
            tier0 = store.load()
            new = self._merge(tier0, confirmed, ts)          # bump existing, collect genuinely-new
            final, deadlock = self._apply_band(tier0, new, tmi, ts)
            store.save(final)
            if deadlock:
                self._flag_deadlock(d, ts)
            return sum(1 for f in final if f.key in {c.key for c in confirmed})
        except Exception:
            return 0
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _acquire(fcntl, fd) -> bool:
        deadline = time.time() + _LOCK_TIMEOUT
        while time.time() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except (BlockingIOError, OSError):
                time.sleep(0.1)
        return False

    def _merge(self, tier0: list[Fact], confirmed: list[Fact], ts: float) -> list[Fact]:
        """Bump facts already in Tier 0 (cross-room boost: union sources, accrue hits/week); return the
        genuinely-new candidates for the admission gate."""
        by_key = {f.key: f for f in tier0}
        fresh = []
        for c in confirmed:
            existing = by_key.get(c.key)
            if existing is not None:
                existing.observe(ts, self._room_id)            # bumps hits/last_seen/week/source
                existing.explicit = existing.explicit or c.explicit
            else:
                fresh.append(c)
        return fresh

    def _apply_band(self, tier0: list[Fact], fresh: list[Fact], tmi, ts: float):
        """Decay → prune → admit/evict, in ONE band fixed from the pre-admit size. Pinned/explicit are
        exempt. Returns (final_facts, deadlock_flag)."""
        hard = getattr(tmi, "tier0_hard_cap", 50)
        band = weights.band_for(len(tier0))  # band chosen ONCE — no mid-cycle recompute (anti-flap)

        kept = [f for f in tier0
                if f.protected or weights.score(f, ts, band.halflife_days) >= band.prune_floor]
        present = {f.key for f in kept}
        deadlock = False
        # Strongest candidates first, so the scarce slots go to the best.
        for cand in sorted(fresh, key=lambda c: weights.score(c, ts, band.halflife_days), reverse=True):
            if cand.key in present:
                continue
            cs = weights.score(cand, ts, band.halflife_days)
            if not cand.protected and cs < band.admit_bar:
                continue  # doesn't clear the band's bar (explicit/pinned bypass the bar)
            if len(kept) >= hard:
                evictable = [f for f in kept if not f.protected]
                if not evictable:
                    deadlock = True  # all protected at the cap — can't make room
                    continue
                victim = min(evictable, key=lambda f: weights.score(f, ts, band.halflife_days))
                if not cand.protected and weights.score(victim, ts, band.halflife_days) >= cs:
                    continue  # the candidate doesn't beat the weakest resident
                kept.remove(victim)
            kept.append(cand)
            present.add(cand.key)
        return kept, deadlock

    @staticmethod
    def _flag_deadlock(tier0_dir_path, ts: float) -> None:
        """Drop a one-shot flag a live turn surfaces: Tier 0 is full of protected facts and can't grow."""
        import json
        try:
            (tier0_dir_path / "cap_alert.json").write_text(
                json.dumps({"at": ts, "reason": "tier0_full_all_protected"}), encoding="utf-8")
        except Exception:
            pass
