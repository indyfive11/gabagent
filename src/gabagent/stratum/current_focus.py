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


def _content_lines(text: str) -> list[str]:
    """Non-blank lines excluding the Current Focus heading itself."""
    return [ln for ln in (text or "").splitlines() if ln.strip() and ln.strip() != HEADING]


def bound_check(old_block: str | None, new_body: str, cfg) -> tuple[bool, str]:
    """Deterministic 'nothing drastic' guard on a Current Focus rewrite. Returns (ok, reason).
    ok=False means REJECT the rewrite and keep the old window. No LLM — a hard floor under the model.
    A first-ever window (no old block) always passes."""
    old_lines = _content_lines(old_block or "")
    new_lines = _content_lines(new_body)
    if not old_lines:
        # nothing to protect yet — only guard against an absurdly large first window
        if len(new_lines) >= cfg.cf_strict:
            return False, f"first window too large ({len(new_lines)} lines ≥ STRICT {cfg.cf_strict})"
        return True, ""
    dropped = (len(old_lines) - len(new_lines)) / len(old_lines)
    if dropped > cfg.cf_max_line_drop_frac:
        return False, (f"rewrite drops {int(dropped * 100)}% of lines "
                       f"(> {int(cfg.cf_max_line_drop_frac * 100)}% bound)")
    old_has_blocked = any("blocked" in ln.lower() for ln in old_lines)
    new_has_blocked = any("blocked" in ln.lower() for ln in new_lines)
    if old_has_blocked and not new_has_blocked:
        return False, "rewrite drops a Blocked line the old window had"
    if len(new_lines) >= cfg.cf_strict:
        return False, f"rewrite exceeds STRICT band ({len(new_lines)} ≥ {cfg.cf_strict})"
    return True, ""


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
