"""Prep-for-Compact routine (Stratum §5.A / §6) — runs at the top of ``_compact_context`` (before the
summary), so the session's insight is swept into durable memory before compaction rewrites the
transcript. Both the auto (0.85) and manual (/compact) paths reach it there.

Mechanism: an in-process AWAITED out-of-band ``complete_simple`` call (the compaction-summary
pattern) — it spends no live context, needs no lock, and is fully ``try/except``-isolated.
Deterministic Python does the snapshot + a hard "nothing drastic" bound + the writes; the model does
the judgment. In ``reviewed`` mode (default) a SECOND, gated adversarial pass — the user's proxy —
vets proposed habit accretions before they land; it fires only when there are habits to judge, so a
Current-Focus-only compaction stays a single call.
"""
from __future__ import annotations

import re
import shutil
import time
from typing import TYPE_CHECKING

from gabagent.stratum import active

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_MIN_TURNS = 4  # mirror _compact_context's <4 bail

_SYSTEM = (
    "You are running the Stratum Prep-for-Compact routine before this coding session's context is "
    "compacted. Preserve what a fresh instance would otherwise have to rediscover. Respond ONLY with "
    "these tagged blocks (omit a block if it has nothing):\n"
    "<CURRENT_FOCUS> the replacement Current Focus WINDOW (not a log): 'Doing (since DATE): …', an "
    "optional 'Blocked:' list, 'Done (recent, last 3, dated):', 'Next:'. Keep it tight. </CURRENT_FOCUS>\n"
    "<HABITS> zero or more lines, each a single observed workflow habit of the USER phrased "
    "observationally ('User tends to …') — only genuine, repeated patterns; never invent one. If a "
    "pattern matches one of the EXISTING HABITS shown to you, echo that heading VERBATIM (do not "
    "rephrase) so it reinforces rather than duplicates. </HABITS>\n"
    "<NOTE> one optional line: durable lesson/gotcha worth keeping. </NOTE>"
)

_REVIEW_SYSTEM = (
    "You are an adversarial reviewer acting as the USER's proxy. Vet each PROPOSED habit observation "
    "before it is written to the user's persistent memory. Reject anything that is: not clearly "
    "supported by evidence (never-fabricate — a habit must be a real repeated pattern, not a guess), "
    "out of scope for this project, redundant with an EXISTING habit, or phrased as a rule rather than "
    "an observation. Prefer REJECT when unsure. For EACH numbered proposal output exactly one line:\n"
    "  N: APPROVE            (keep as-is)\n"
    "  N: TRIM: <better phrasing>   (keep, but with this safer/tighter wording)\n"
    "  N: REJECT             (do not store)\n"
    "Add a line 'ESCALATE: <question>' ONLY for a genuine scope conflict the user must resolve."
)


def _between(text: str, tag: str) -> str:
    if not text:
        return ""
    open_t, close_t = f"<{tag}>", f"</{tag}>"
    i = text.find(open_t)
    if i == -1:
        return ""
    start = i + len(open_t)
    j = text.find(close_t, start)
    return (text[start:j] if j != -1 else text[start:]).strip()


async def run(ctx: AgentContext) -> None:
    """Best-effort, isolated. Never raises, never blocks the compaction that follows."""
    if not active(ctx):
        return
    try:
        await _run_inner(ctx)
    except Exception as e:  # pragma: no cover - defensive isolation
        _log(ctx, f"Stratum compact-prep skipped: {e}")


async def _run_inner(ctx: AgentContext) -> None:
    from gabagent.api.models import ChatMessage
    from gabagent.config.paths import observed_habits_file
    from gabagent.session.memory import MemoryManager
    from gabagent.stratum import current_focus, observer
    from gabagent.stratum.observed import ObservedStore

    msgs = ctx.session.messages()
    turns = [m for m in msgs if m.role in ("user", "assistant") and m.content]
    if sum(1 for m in turns if m.role == "user") < _MIN_TURNS:
        return

    cfg = ctx.config.stratum
    model = cfg.model or None
    mem = MemoryManager(ctx.cwd)
    store = ObservedStore(observed_habits_file())
    memory_text = mem.load()
    existing = [h.heading for h in store.load()]

    # 1) snapshot the memory tree (our rollback; the .pre-compact backup covers only the JSONL).
    _snapshot(ctx)

    # 2) purpose-built out-of-band sweep (does NOT consume the live context window).
    transcript = "\n".join(f"{m.role}: {m.content}" for m in turns[-40:])
    cf_block = current_focus.extract_block(memory_text)
    context = (
        f"=== CURRENT `memory.md` ===\n{memory_text or '(empty)'}\n\n"
        f"=== CURRENT FOCUS BLOCK ===\n{cf_block or '(none yet)'}\n\n"
        f"=== EXISTING HABITS (echo a heading verbatim to reinforce) ===\n"
        f"{chr(10).join(existing) or '(none)'}\n\n"
        f"=== DETERMINISTIC SIGNALS THIS SESSION ===\n{observer.summary(ctx) or '(none)'}\n\n"
        f"=== RECENT CONVERSATION ===\n{transcript}"
    )
    out = await ctx.client.complete_simple(
        [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=context)],
        model=model,
    )

    new_cf = _between(out, "CURRENT_FOCUS")
    habits_raw = _between(out, "HABITS")
    note = _between(out, "NOTE")
    if not (new_cf or habits_raw or note):
        _log(ctx, "compact-prep: model returned no parseable blocks — memory left unchanged.")
        return

    # 3a) Current Focus — apply only if it passes the deterministic 'nothing drastic' bound.
    if new_cf:
        ok, reason = current_focus.bound_check(cf_block, new_cf, cfg)
        if ok:
            mem.rewrite(current_focus.upsert_block(memory_text, new_cf))
            mem.health_check()
        else:
            _log(ctx, f"compact-prep: Current Focus rewrite rejected ({reason}) — kept existing window.")

    # 3b) Habits — proxy-reviewed in 'reviewed' mode (only when there ARE habits to judge).
    if habits_raw:
        candidates = [ln.strip().lstrip("-*").strip() for ln in habits_raw.splitlines()]
        candidates = [c for c in candidates if len(c) >= 6]
        if candidates:
            if cfg.observation_mode == "reviewed":
                approved, escalations = await _review(ctx, cfg, candidates, existing, model)
                for q in escalations:
                    _log(ctx, f"compact-prep ESCALATE (needs your call): {q}")
            else:  # "auto" — trust the deterministic layer, skip the reviewer round-trip
                approved = candidates
            for obs in approved:
                store.accrete(obs, origin="observed", state="accreting")

    # 3c) Note — a single dated lesson line (bounded by its own append; low risk).
    if note:
        mem.append(f"[stratum] {note.splitlines()[0][:300]}")
        mem.health_check()

    # 4) light maintenance (cap + advance).
    store.prune(cfg.tier05_soft_cap, cfg.tier05_hard_cap, cfg.tier05_halflife_days)
    store.advance(cfg.adv_days, cfg.adv_hits, cfg.adv_weeks)

    # signals consumed; reset for the next segment.
    ctx.stratum_signals.clear()
    ctx.stratum_seen_calls.clear()


async def _review(ctx: AgentContext, cfg, candidates: list[str], existing: list[str], model):
    """The user's proxy: one adversarial pass over proposed habits → (approved, escalations).
    Fail-safe: a candidate with no clear APPROVE/TRIM verdict is NOT stored (never-fabricate)."""
    from gabagent.api.models import ChatMessage
    listing = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(candidates))
    context = (
        f"=== PROJECT SCOPE ===\n{_scope_excerpt(ctx)}\n\n"
        f"=== EXISTING HABITS ===\n{chr(10).join(existing) or '(none)'}\n\n"
        f"=== PROPOSED NEW HABIT OBSERVATIONS ===\n{listing}"
    )
    try:
        out = await ctx.client.complete_simple(
            [ChatMessage(role="system", content=_REVIEW_SYSTEM),
             ChatMessage(role="user", content=context)],
            model=model,
        )
    except Exception as e:
        _log(ctx, f"compact-prep: reviewer call failed ({e}) — no habits stored this pass.")
        return [], []

    approved: list[str] = []
    escalations: list[str] = []
    for line in out.splitlines():
        ls = line.strip()
        if ls.upper().startswith("ESCALATE:"):
            q = ls[len("ESCALATE:"):].strip()
            if q:
                escalations.append(q)
            continue
        m = re.match(r"^(\d+)\s*[:.\)]\s*(APPROVE|TRIM|REJECT)\b\s*:?\s*(.*)$", ls, re.I)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        verdict = m.group(2).upper()
        rest = m.group(3).strip()
        if 0 <= idx < len(candidates):
            if verdict == "APPROVE":
                approved.append(candidates[idx])
            elif verdict == "TRIM":
                approved.append(rest or candidates[idx])
            # REJECT (or anything unparsed) → not stored
    return approved, escalations


def _scope_excerpt(ctx: AgentContext) -> str:
    """First ~60 lines of the project's plan/charter, best-effort, for the reviewer's scope check."""
    try:
        for name in ("ROADMAP.md", "CLAUDE.md", "README.md"):
            p = ctx.cwd / name
            if p.exists():
                return "\n".join(p.read_text(encoding="utf-8").splitlines()[:60])
    except Exception:
        pass
    return "(no plan/charter doc found)"


def _snapshot(ctx: AgentContext) -> None:
    """Copy the memory tree aside before writing, and prune old snapshots (keep newest N per file)."""
    try:
        from gabagent.config.paths import memory_file, observed_habits_file
        keep = int(getattr(ctx.config.stratum, "snapshot_keep", 5))
        ts = int(time.time())
        for path in (memory_file(ctx.cwd), observed_habits_file()):
            if path.exists():
                shutil.copy2(path, path.with_name(f"{path.stem}.pre-stratum-{ts}{path.suffix}"))
            _prune_snapshots(path, keep)
    except Exception:
        pass


def _prune_snapshots(path, keep: int) -> None:
    try:
        snaps = sorted(
            path.parent.glob(f"{path.stem}.pre-stratum-*{path.suffix}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in snaps[max(0, keep):]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def _log(ctx: AgentContext, text: str) -> None:
    try:
        from gabagent.tui.renderer import console
        console.print(f"[dim]{text}[/dim]", markup=True)
    except Exception:
        pass
