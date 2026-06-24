"""send_to_builder — voice/text-dispatched coding builder (phase 0: text-first).

A dispatched coding task is run by a headless `claude -p` in a chosen project, DETACHED in its own
systemd scope so it outlives the brain that dispatched it. The result is a GROUND-TRUTH summary
(verified exit code + git delta, never the model's self-report) written to a flock'd job store and
surfaced back in the TUI.

Phase 0 contract (Rob's M5 ruling = reviewable-diff): the builder has full LOCAL authority
(write / run / commit-local) but MUST NOT push — it produces a local branch/diff Rob reviews and
pushes himself. Autonomous push unlocks only at phase 4 (interactive-ask + ground-truth verified).
"""
