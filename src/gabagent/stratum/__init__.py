"""Stratum — native memory-management subsystem (thin additions to the existing memory surface).

Three additions, all gated behind ``config.stratum.enabled`` (default False ⇒ byte-identical to
today): a Current Focus window inside the per-cwd ``memory.md`` (:mod:`current_focus`), a
Prep-for-Compact routine that runs before context compaction (:mod:`compact_prep`), and a
subordinate Observed-Habits store (:mod:`observed`) fed by an in-memory per-turn observer
(:mod:`observer`). Session-start injection is assembled by :mod:`inject`; agent-callable tools live
in :mod:`tools`.

Design record: docs/STRATUM.md. The subsystem is coding-lane only and is gated OFF inside
sub-agents (``ctx.is_subagent``). Nothing here runs — and no files are created — while disabled.
"""
from __future__ import annotations


def active(ctx) -> bool:
    """True iff Stratum should act for this context: enabled in config AND not a sub-agent.

    The single gate every lifecycle seam checks, so ``enabled=False`` (and every sub-agent) stays
    byte-identical to today. Defensive against a config object without the block.
    """
    try:
        if getattr(ctx, "is_subagent", False):
            return False
        return bool(getattr(ctx.config, "stratum", None) and ctx.config.stratum.enabled)
    except Exception:
        return False
