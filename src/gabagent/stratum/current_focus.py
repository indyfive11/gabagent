"""Current Focus window (Stratum §4.A) — a single ``## Current Focus`` block at the top of the
per-cwd ``memory.md``. It is a *window* (a snapshot of now), replaced at each compact-prep, never
appended. Everything here is deterministic: parse, measure LINES (never bytes), upsert. The rewrite
judgment is the model's (compact-prep); the size-check only measures and emits a reminder.
"""
from __future__ import annotations

HEADING = "## Current Focus"


def extract_block(memory_text: str) -> str | None:
    """Return the ``## Current Focus`` block (heading + body up to the next ``## `` or EOF), or None."""
    if not memory_text:
        return None
    lines = memory_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == HEADING), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    return "\n".join(lines[start:end]).rstrip()


def upsert_block(memory_text: str, block_body: str) -> str:
    """Replace the existing Current Focus block, or insert a fresh one at the very top. ``block_body``
    is the content BELOW the heading (the heading is managed here so it stays canonical)."""
    body = block_body.strip()
    new_block = f"{HEADING}\n\n{body}\n" if body else f"{HEADING}\n"
    text = memory_text or ""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == HEADING), None)
    if start is None:
        prefix = new_block.rstrip() + "\n"
        return prefix + ("\n" + text.lstrip("\n") if text.strip() else "\n")
    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    rebuilt = lines[:start] + new_block.rstrip().splitlines() + [""] + lines[end:]
    return "\n".join(rebuilt).rstrip() + "\n"


def _band(n: int, notice: int, firm: int, strict: int) -> str | None:
    if n >= strict:
        return "STRICT"
    if n >= firm:
        return "FIRM"
    if n >= notice:
        return "NOTICE"
    return None


def size_reminder(memory_text: str, cfg) -> str:
    """A one-line reminder when the Current Focus block or the whole memory file is over its LINE
    budget. Empty string when within budget. Measures LINES, never bytes."""
    if not memory_text:
        return ""
    block = extract_block(memory_text)
    cf_lines = len(block.splitlines()) if block else 0
    idx_lines = len(memory_text.splitlines())
    cf = _band(cf_lines, cfg.cf_notice, cfg.cf_firm, cfg.cf_strict)
    idx = _band(idx_lines, cfg.idx_notice, cfg.idx_firm, cfg.idx_strict)
    parts = []
    if cf:
        parts.append(f"Current Focus is {cf} ({cf_lines} lines) — bring it back to a window at compact-prep")
    if idx:
        parts.append(f"memory.md is {idx} ({idx_lines} lines) — fold detail out at compact-prep")
    if not parts:
        return ""
    return "MEMORY SIZE: " + "; ".join(parts) + " (measured in LINES)."
