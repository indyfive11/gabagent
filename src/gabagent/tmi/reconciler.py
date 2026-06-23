"""TmiReconciler — the automatic consolidate + prune pass for per-room (Tier 1) memory.

Rides the same detached shutdown trigger as persona reflection (it outlives the dying brain), and is
"persona reflection, generalized to memory, per room": read the recent conversation + this room's
current facts, ask the top rung for the full updated durable set, then merge that into the room's
Tier-1 fact store — bumping recurrence metadata on facts that persist, adding new ones, and PRUNING
non-pinned facts that dropped out. P2 scope: NO Tier-0 escalation, NO weighting, NO cross-process lock
(Tier 1 is per-room single-writer). Best-effort; never raises, never blocks shutdown.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gabagent.tmi.facts import Fact, FactStore, iso_week, normalize

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Keep Tier 1 crisp — a per-room note set, not a transcript. The model is asked for at most this many.
_MAX_ROOM_FACTS = 30
_MIN_TURNS = 4
_TRANSCRIPT_TURNS = 40

_RECONCILE_SYSTEM = (
    "You maintain Aria's durable MEMORY for ONE room/space she serves. You are NOT talking to the user "
    "— you are updating her private per-room notes. Read this room's current facts and the latest "
    "conversation, then output the FULL updated set of durable, room-relevant facts.\n\n"
    "What belongs: standing user preferences and habits, names/relationships, recurring routines, and "
    "stable context about this space. What does NOT: one-off chitchat, transient state, and "
    "speech-to-text noise (the transcript is noisy — mine signal, not mis-hearings).\n"
    "Keep it SMALL and crisp: at most ~"
    f"{_MAX_ROOM_FACTS} facts, each ONE short line. Drop facts no longer true. Reinforce facts the "
    "conversation supports. This set REPLACES the old one.\n"
    "Prefix a line with [explicit] ONLY when the user DIRECTLY instructed it — said remember this, or "
    "stated an always/never rule (e.g. '[explicit] always dim the lights at night', '[explicit] I'm "
    "allergic to nuts'). Most facts are observed, NOT explicit — do not over-mark.\n\n"
    "Output EXACTLY this and nothing else:\n"
    "<FACTS>\n"
    "(the full updated fact set as short markdown bullets — one fact per line, [explicit] prefix where due)\n"
    "</FACTS>\n"
    "<NOTE>\n"
    "(one short line on what changed, or 'no change')\n"
    "</NOTE>"
)


class TmiReconciler:
    def __init__(self, room_id: str | None) -> None:
        self._room_id = (room_id or None) if (room_id or "").strip() else None

    async def reconcile_from_ctx(self, ctx: "AgentContext", now: float | None = None) -> bool:
        """Consolidate this room's Tier-1 memory from the recent session. Returns True if it wrote.
        Inert (returns False) unless config.tmi.enabled. Never raises."""
        try:
            cfg = getattr(ctx, "config", None)
            if cfg is None or not getattr(getattr(cfg, "tmi", None), "enabled", False):
                return False
            msgs = ctx.session.messages()
            turns = [m for m in msgs if m.role in ("user", "assistant") and m.content]
            user_turns = sum(1 for m in turns if m.role == "user")
            if user_turns < _MIN_TURNS:
                from gabagent.tmi.observe import tmi_log
                tmi_log("reconcile", room=self._room_id or "default",
                        skipped="too_few_turns", user_turns=user_turns)
                return False  # nothing worth consolidating
        except Exception:
            return False

        from gabagent.config.paths import room_dir
        from gabagent.tmi.observe import tmi_log
        store = FactStore(room_dir(self._room_id) / "facts.jsonl")
        existing = store.load()

        new_lines = await self._consolidate(ctx, turns)
        if new_lines is None:
            tmi_log("reconcile", room=self._room_id or "default", skipped="llm_failed")
            return False  # the model call failed — leave the store untouched

        ts = now if now is not None else time.time()
        merged = self._merge(existing, new_lines, ts)
        store.save(merged)
        self._render_index(room_dir(self._room_id) / "memory.md", merged)
        old_keys = {f.key for f in existing}
        new_keys = {f.key for f in merged}
        tmi_log("reconcile", room=self._room_id or "default",
                before=len(existing), after=len(merged),
                added=len(new_keys - old_keys), pruned=len(old_keys - new_keys),
                kept=len(old_keys & new_keys),
                explicit=sum(1 for f in merged if f.explicit))
        return True

    # -- the LLM consolidate pass -----------------------------------------
    async def _consolidate(self, ctx, turns) -> list[str] | None:
        from gabagent.api.models import ChatMessage
        from gabagent.persona.manager import PersonaManager, _between
        from gabagent.tmi.manager import TmiManager

        transcript = "\n".join(f"{m.role}: {m.content}" for m in turns[-_TRANSCRIPT_TURNS:])
        # The room's CURRENT notes via the same read-through the recall path uses — so first-run legacy
        # cwd memory is folded into the consolidated set rather than silently overwritten.
        current = TmiManager(ctx).tier1_brief() or "(none yet)"
        context = (
            f"=== ROOM ===\n{self._room_id or 'default'}\n\n"
            f"=== THIS ROOM'S CURRENT FACTS ===\n{current}\n\n"
            f"=== LATEST CONVERSATION (speech-to-text, noisy) ===\n{transcript}"
        )
        prompt = [
            ChatMessage(role="system", content=_RECONCILE_SYSTEM),
            ChatMessage(role="user", content=context),
        ]
        try:
            client, model, effort = PersonaManager._reflection_client(ctx)  # top rung, like persona
            out = await client.complete_simple(prompt, model=model, effort=effort)
        except Exception:
            return None
        block = _between(out, "FACTS")
        lines = [ln.strip() for ln in block.splitlines() if normalize(ln)]
        return lines[:_MAX_ROOM_FACTS]

    # -- merge new set into the store, preserving + pruning ----------------
    def _merge(self, existing: list[Fact], new_lines: list[str], ts: float) -> list[Fact]:
        by_key = {f.key: f for f in existing}
        wk = iso_week(ts)
        out: list[Fact] = []
        seen_keys = set()
        for line in new_lines:
            text = line.lstrip("-*•").strip()
            explicit = False
            if text.lower().startswith("[explicit]"):
                explicit, text = True, text[len("[explicit]"):].strip()
            key = normalize(text)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            f = by_key.get(key)
            if f is not None:
                f.text = text  # keep the latest phrasing
                f.explicit = f.explicit or explicit  # sticky: once explicit, stays explicit
                f.observe(ts, self._room_id)
            else:
                f = Fact(text=text, sources=[self._room_id or "default"], first_seen=ts,
                         last_seen=ts, hits=1, seen_weeks=[wk], explicit=explicit)
            out.append(f)
        # Keep PROTECTED facts the model dropped (pinned or explicit never prune by omission);
        # everything else not re-emitted is pruned.
        for f in existing:
            if f.protected and f.key not in seen_keys:
                out.append(f)
        return out

    @staticmethod
    def _render_index(path, facts: list[Fact]) -> None:
        """Render the human/recall view (memory.md) that the Tier-1 brief reads — just the fact lines."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(f"- {f.text}" for f in facts) + ("\n" if facts else ""),
                            encoding="utf-8")
        except Exception:
            pass
