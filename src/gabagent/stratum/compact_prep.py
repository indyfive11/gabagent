"""Prep-for-Compact routine (Stratum §5.A / §6) — runs at the top of ``_compact_context`` (before the
summary), so the session's insight is swept into durable memory before compaction rewrites the
transcript. Both the auto (0.85) and manual (/compact) paths reach it there.

Mechanism (per the hardened design): an in-process AWAITED out-of-band ``complete_simple`` call
(the same pattern the compaction summary uses) — it spends no live context, needs no lock, and is
fully ``try/except``-isolated. Deterministic Python does the snapshot + the writes; the model does
the judgment (rewrite the Current Focus window, propose habit observations).
"""
from __future__ import annotations

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
    "observationally ('User tends to …') — only genuine, repeated patterns; never invent one. </HABITS>\n"
    "<NOTE> one optional line: durable lesson/gotcha worth keeping. </NOTE>"
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
        try:
            from gabagent.tui.renderer import console
            console.print(f"[dim]Stratum compact-prep skipped: {e}[/dim]", markup=True)
        except Exception:
            pass


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
    mem = MemoryManager(ctx.cwd)
    store = ObservedStore(observed_habits_file())
    memory_text = mem.load()

    # 1) snapshot the memory tree (our own rollback; the .pre-compact backup covers only the JSONL).
    _snapshot(ctx)

    # 2) purpose-built out-of-band call (does NOT consume the live context window).
    transcript = "\n".join(f"{m.role}: {m.content}" for m in turns[-40:])
    sig = observer.summary(ctx)
    cf_block = current_focus.extract_block(memory_text) or "(none yet)"
    context = (
        f"=== CURRENT `memory.md` ===\n{memory_text or '(empty)'}\n\n"
        f"=== CURRENT FOCUS BLOCK ===\n{cf_block}\n\n"
        f"=== DETERMINISTIC SIGNALS THIS SESSION ===\n{sig or '(none)'}\n\n"
        f"=== RECENT CONVERSATION ===\n{transcript}"
    )
    prompt = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(role="user", content=context),
    ]
    out = await ctx.client.complete_simple(prompt)

    # 3) apply writes (deterministic). Log — never silently swallow — a zero-tag parse.
    new_cf = _between(out, "CURRENT_FOCUS")
    habits_raw = _between(out, "HABITS")
    note = _between(out, "NOTE")
    if not (new_cf or habits_raw or note):
        _log(ctx, "compact-prep: model returned no parseable blocks — memory left unchanged.")
        return

    if new_cf:
        mem.rewrite(current_focus.upsert_block(memory_text, new_cf))
        mem.health_check()

    if habits_raw and cfg.observation_mode in ("surface", "auto"):
        state = "accreting" if cfg.observation_mode == "auto" else "candidate"
        for line in habits_raw.splitlines():
            obs = line.strip().lstrip("-*").strip()
            if len(obs) >= 6:
                store.accrete(obs, origin="observed", state=state)

    if note:
        mem.append(f"[stratum] {note}")
        mem.health_check()

    # 4) light maintenance pass (cap + advance).
    store.prune(cfg.tier05_soft_cap, cfg.tier05_hard_cap, cfg.tier05_halflife_days)
    store.advance(cfg.adv_days, cfg.adv_hits, cfg.adv_weeks)

    # signals consumed; reset for the next segment.
    ctx.stratum_signals.clear()
    ctx.stratum_seen_calls.clear()


def _snapshot(ctx: AgentContext) -> None:
    try:
        from gabagent.config.paths import memory_file, observed_habits_file
        ts = int(time.time())
        for path in (memory_file(ctx.cwd), observed_habits_file()):
            if path.exists():
                shutil.copy2(path, path.with_name(f"{path.stem}.pre-stratum-{ts}{path.suffix}"))
    except Exception:
        pass


def _log(ctx: AgentContext, text: str) -> None:
    try:
        from gabagent.tui.renderer import console
        console.print(f"[dim]{text}[/dim]", markup=True)
    except Exception:
        pass
