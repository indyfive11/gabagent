"""Tiered-Memory-Indexing (TMI): a self-reconciling, cross-room memory layer for Aria.

Tier 0 — shared across ALL Aria processes (the 'one Aria' durable identity/facts).
Tier 1 — per-room durable notes (keyed on room_id).
Tier 2 — the recent message window (owned by the turn builder, not a new store).

P1 ships the READ facade only (recall blending); consolidation/escalation land in later phases.
Everything is gated behind config.tmi.enabled — OFF ⇒ exact current persona/memory behavior.
"""
