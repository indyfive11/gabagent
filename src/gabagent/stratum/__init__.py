"""Stratum — native memory-management subsystem (thin additions to the existing memory surface).

Three additions, all gated behind ``config.stratum.enabled`` (default False ⇒ byte-identical to
today): a Current Focus window inside the per-cwd ``memory.md`` (:mod:`current_focus`), a
Prep-for-Compact routine that runs before context compaction (:mod:`compact_prep`), and a
subordinate Observed-Habits store (:mod:`observed`) fed by an in-memory per-turn observer
(:mod:`observer`). Session-start injection is assembled by :mod:`inject`; the on-demand human-review
audit is the ``/reconcile`` slash command. Stratum exposes NO model-facing tools — the model never
proactively invokes a memory action; it runs only as the background compact-prep routine.

Design record: docs/STRATUM.md. The subsystem is coding-lane only and is gated OFF inside
sub-agents (``ctx.is_subagent``) and on the voice path (``ctx.voice_mode``). Nothing here runs — and
no files are created — while disabled.
"""
from __future__ import annotations


def active(ctx) -> bool:
    """True iff Stratum should act for this context: enabled in config AND not a sub-agent.

    The single gate every lifecycle seam checks, so ``enabled=False`` (and every sub-agent) stays
    byte-identical to today. Defensive against a config object without the block.

    ``enabled`` is checked FIRST so a disabled install does the least possible work on the hot path
    (``active()`` runs ≥2×/turn). No ``voice_mode`` check: Stratum's seams live only in the coding
    loop (``agent/loop.py``); the voice runner (``voice/turn.py``) wires none of them, so voice
    exclusion is structural, not a gate here. When the voice loop ever wires a Stratum seam, that
    seam gates on its own task/context — see docs/STRATUM.md.
    """
    try:
        cfg = getattr(ctx.config, "stratum", None)
        if not (cfg and cfg.enabled):
            return False
        if getattr(ctx, "is_subagent", False):
            return False
        return True
    except Exception:
        return False
