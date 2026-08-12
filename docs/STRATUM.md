# Stratum — native memory-management subsystem for gabagent

**Status:** implemented (v2 "reviewed"). Reachable branch of [ROADMAP.md](../ROADMAP.md) — not a second
plan root. Default `observation_mode="reviewed"`: proposed habit accretions are vetted by one
adversarial reviewer (the user's proxy) before they land; a deterministic bound guards every
Current-Focus rewrite; there are **no model-facing tools** (the model never invokes a memory action);
a human-invoked `/reconcile` gives an on-demand read-only audit.
**Scope:** thin additions to gabagent's existing memory surface. Explicitly **not** a spine-wide
unification (see §2). Coding-lane only — gated off in sub-agents; voice exclusion is structural (the
voice runner wires none of Stratum's seams).

---

## 1. What this is

Stratum is a memory-management architecture: a tiered memory tree, an auto-accreting observed-habits
store (subordinate, never authoritative), a Prep-for-Compact routine, and a plan-tree anti-drift
check. gabagent is itself a stateless-per-session agent, so this brings three of Stratum's paradigms
in **natively** — as in-process routines wired to gabagent's own lifecycle — rather than as external
shell hooks. The guiding principle is unchanged from the source system: *the machinery measures and
emits; the model acts.* Here that becomes: **deterministic Python measures/enforces; the agent's LLM
does the judgment.**

Three thin additions to the existing per-cwd `memory.md` surface (`session/memory.py`):

- **A. Current Focus window** — a `## Current Focus` block schema inside the existing `memory.md`.
- **B. Compact-prep routine** — a memory-preservation pass that runs before context compaction.
- **C. Observed-habits store** — a new, subordinate store of workflow-habit observations; starts
  empty, accretes over time, promotion to durable rules is always user-gated.

Plus a naming fix: the new store is called **"Observed Habits"** — never "TMI" — to avoid collision
with the existing `tmi/` module (a different, room-keyed subsystem).

## 2. Boundary — what this is NOT

An earlier design unified gabagent's several memory systems into one scope-keyed engine and retired
the others. Adversarial review against source showed that over-reached: it fought two deliberate
designs already in the code. This spec deliberately does **not**:

- **No single unified memory tree / "Scope" abstraction.** `memory.md` stays per-cwd; the voice
  brain's room memory stays room-keyed.
- **No retiring the persona layer or the `tmi/` module.** The persona layer stays **global and
  user-invisible** by design (`persona/manager.py:1,46,82`); `tmi/`'s cross-room fact escalation
  stays intact (it *deliberately* reads one room and writes a shared store —
  `escalator.py:56-58,115-136` — which a scope-isolation invariant would have broken).
- **No cross-store migration.** Nothing reshapes `memory.md`; the observed-habits store starts empty.
  This is why `enabled=False` is genuinely a no-op (§5.C).
- **No voice-lane observer.** The observed-habits feature is **coding-lane only**; the voice turn
  runner (`voice/turn.py`, its own `_voice_system()` builder) is untouched.
- **Deep Reconcile stays a manual routine.** A native `/reconcile` is a possible later add, not here.

## 3. Why native beats external hooks

The "do more than external hooks can" value lives in the *mechanism*, not in unification:

| Capability | Delivered by | Why it beats an external hook |
|---|---|---|
| Deterministic caps / size / staleness | Python size-check at session start + compact-prep | an external hook can only remind; the model must remember the discipline |
| Live observation | in-process per-turn signal capture (§5.B) | an external stop-hook cannot see mid-turn / tool-call turns |
| Real compact-prep routine | runs in the runtime (§5.A) | an external pre-compact hook injects a checklist and hopes it ran |
| Structure validation | validated on write | external tooling silently mis-parses |

## 4. The three additions

### A. Current Focus window
A single `## Current Focus` block at the top of `memory.md`: Doing / Blocked / Done (≤3, dated) /
Next. It is a **window**, replaced at each compact-prep, never appended. A deterministic size-check
at session start measures LINES (bands: Current Focus 150/300/600; whole file 400/700/1000) and, when
over budget, surfaces a reminder via the existing system-message injection pattern (`loop.py:217`).
The check *measures and emits*; the model rewrites at the next compact-prep. No new file, no auto-edit.

### B. Compact-prep routine
Runs the five steps: sweep→write durable memory; tier+index (fold-first, small per-event budget);
rewrite the Current Focus window; observed-habits pass; verify-one-plan-tree. Deterministic parts in
Python (snapshot, size measurement, habit scoring, staleness flags, mechanical trim); judgment parts
in the model (sweep, fold, window rewrite, conflict-marking, plan verify). Trigger + mechanism: §5.A.

### C. Observed-habits store
- File: `data_dir()/stratum/observed_habits.md`, machine-wide/SHARED by default.
- Record: a `##` immutable heading (the observation) + fields `hits / seen_weeks / conflict_count /
  state / origin`.
- **Scoring:** reuse only the general `weights.decay()` (`weights.py:51-56`) and the `log1p(hits)`
  shape — **not** `weights.score()` verbatim (it carries a cross-room term and a volatility term that
  are meaningless for coding habits, and has no conflict penalty). Purpose-built score:
  `decay(last_seen) + log1p(hits) + (definitive?1:0) − conflict_count`. Caps soft 40 / hard 75;
  eligibility gate 30d / 5 hits / 3 distinct weeks / 0 conflicts; **promotion to a durable rule is
  always user-gated.**
- Loaded at session start, ranked, top ~35 / ~3 KB, injected as an explicit **SUBORDINATE** block
  that never overrides durable rules or explicit instructions.
- **Starts empty** — no seeding from any existing store.

## 5. Hard requirements (implementation-critical)

### 5.A — Compact-prep trigger + mechanism
gabagent auto-compacts when `token_estimate > max_context_tokens * 0.85` (`loop.py:369`); the soft
warn ratio is 0.70 (`loop.py:14`). Two requirements:

1. **Runs at the top of `_compact_context`** (after the `<4`-message bail, before the summary is
   built). This single placement covers *both* the auto trigger (the 0.85 path calls
   `_compact_context`) and the manual `/compact` (which calls it directly) — so there is no
   skip-over of an earlier 0.70-only branch and no separate `prep_done` flag to manage. The routine
   self-gates (`active()`), so a disabled install / sub-agent is a no-op.
2. **Awaited out-of-band `complete_simple` — not a detached child, not in-session.** The routine runs
   as an in-process **awaited** out-of-band LLM call, exactly the compaction-summary pattern
   (`ctx.client.complete_simple(<transcript + current memory + instructions>)`, `loop.py:136`); it
   parses structured output and Python applies the writes under snapshot. This spends **no live
   context** (purpose-built message list, not the overflowing session), needs **no lock**
   (event-loop-serialized), and has trivial `try/except` isolation.
   - *Not a detached child* (`reflect_detached.py`): that exists to win a shutdown race which does not
     exist mid-session, and a detached writer on the shared store reintroduces the exact contention
     the cross-room path needs an fcntl lock for (`escalator.py:122-128`).
   - *Not an in-session directive:* that would consume the 0.70 budget.
   - *Heavier in-process fallback* if a step ever needs real tool-use: `sub_agent._spawn_foreground`
     (`sub_agent.py:49`). Start with `complete_simple`.
3. **Own snapshot.** Before the routine writes, copy the memory tree aside (`memory.md` +
   `observed_habits.md`, timestamped) — independent of the `.pre-compact-{ts}` session-JSONL backup,
   which copies only the transcript, after the summary (`loop.py:143-147`), and does not snapshot the
   memory tree.
4. **Error isolation.** Wrapped `try/except`: any failure logs, leaves memory at the snapshot, and
   must not block the subsequent compaction or crash the loop.
5. **No-op gate.** Skip trivial sessions (mirror the `<4`-message bail, `loop.py:114-116`).
6. **Manual paths** (`/compact`, `slash/commands.py:90`; "prep for compact") run the same idempotent,
   snapshotted routine.

### 5.B — Observer seam
The `run_stop` seam fires only on terminal text-only turns (`loop.py:448-450`) — it never sees
tool-call turns, and the voice runner never calls it. So:

1. **Cheap per-turn signal capture, deterministic, zero IO.** A new seam after every assistant turn
   *including tool-call turns* (where results are appended, `loop.py:460-471`, not at `run_stop`)
   appends signals to an **in-memory list on `ctx`** (never disk — zero hot-path IO). Truly
   deterministic seam-level signals: **tool rejection / permission-denial**, and **identical
   `(tool, args)` seen N times in a session** (a redo/oscillation marker). "User corrections" and
   "behavioral patterns" are *not* seam signals — they are model interpretations over the raw log
   (step 2); the seam only logs raw events.
2. **Interpretation at compact-prep, off the hot path.** The routine reads the in-memory log +
   transcript and proposes candidate habits. `surface` mode (default): list candidates for
   confirmation — nothing about the user is asserted unverified. `auto` mode: accrete directly
   (subordinate; promotion still user-gated).
3. **Voice lane excluded.** The voice runner builds its own context and is untouched; both the
   observer (write) and the store injection (read) structurally miss voice. (Keep the brain's cwd a
   non-Stratum scope, so a `## Current Focus` block never surfaces in voice via its `memory.md`
   read, `voice/turn.py:176-183`.)

### 5.C — No model-facing tools / no-op honesty
Stratum exposes **no** model-facing tools. (An earlier revision registered `current_focus_update` /
`observed_habit_note` / `stratum_status`; because the voice tool filter `_voice_tool_schemas`
(`turn.py:318`) is a denylist that only strips `bash`/`run_shell`, those tools leaked to the Aria
voice model — the live bug that prompted this redesign. Removing them fixes it at the root.) Therefore:

1. **The model never invokes a Stratum action.** Stratum runs only as the compact-prep routine + the
   human-invoked `/reconcile`. Nothing to leak into any lane.
2. **Call-site gating, not registration.** The loop seams (compact-prep trigger, observer) guard on
   `active(ctx)` — which is False when disabled or in a sub-agent. Voice exclusion is **structural, not a
   gate here**: the seams live only in the coding loop (`agent/loop.py`); the voice runner
   (`voice/turn.py`) wires none of them, so a voice ctx never reaches `active()`. (When the voice loop
   ever wires a Stratum seam, that seam gates on its own task/context — 2026-08-12 charter re-baseline.)
3. **`enabled=False` is genuinely byte-identical:** nothing reshaped, no migration, no tools registered,
   the store is created lazily (only on first accretion when enabled). Matches the pure-runtime-gate
   precedent of the optional integrations. **Test:** an `enabled=False` session touches zero new files.

## 6. Config — `StratumConfig` (all defaults = historical no-op)

```
stratum.enabled: bool = False                 # OFF = byte-identical to today
stratum.observation_mode: "reviewed"|"auto" = "reviewed"  # reviewed = proxy vets accretions; auto = deterministic bound only
stratum.model: str = ""                       # optional cheap model for the out-of-band sweep/reviewer calls ("" = inherit)
stratum.compact_prep_ratio: float = 0.70      # documentary; trigger is the top of _compact_context
stratum.cf_max_line_drop_frac: float = 0.5    # deterministic "nothing drastic" bound on a CF rewrite
stratum.snapshot_keep: int = 5                # retention for *.pre-stratum-* memory snapshots
stratum.tier05_halflife_days: int = 30
stratum.tier05_caps: (soft=40, hard=75)
stratum.tier05_gate: (adv_days=30, adv_hits=5, adv_weeks=3)
stratum.cf_line_bands:  (150, 300, 600)       # Current Focus, LINES
stratum.idx_line_bands: (400, 700, 1000)      # whole memory.md, LINES
stratum.audit_threshold_days: int = 90        # observed-store last_seen (light)
stratum.repeat_signal_threshold / edit_churn_threshold: int = 3   # deterministic observer signals
```

Follows the codebase pattern (nested `BaseModel` via `Field(default_factory)`; the flag is nested so
it is set in `settings.json`, not via env). **Installer parity in the same change:** `docs/INSTALL.md`
wiring. No new system dependency; no provisioning (lazy store), so the no-op default genuinely holds.

**Observation modes.** `reviewed` (default): at compact-prep, proposed habit accretions pass through
one adversarial reviewer (the user's proxy) that vets them against scope + existing memory before they
land — and it fires ONLY when there are habits to judge, so a Current-Focus-only compaction stays a
single call. `auto`: skip the reviewer, trust the deterministic diff-bound (cheapest). Either way the
deterministic `cf_max_line_drop_frac` bound guards every Current-Focus rewrite for free, and promotion
of a habit to a durable rule is always user-gated.

## 7. Package layout (as built)

```
src/gabagent/stratum/
  __init__.py         # active(ctx) gate: enabled AND not sub-agent (voice exclusion is structural — voice wires no seam)
  current_focus.py    # window schema: parse / measure LINES / rewrite + bound_check (nothing-drastic guard)
  observed.py         # observed-habits store: record format, scoring, caps/decay, eligibility gate, state machine
  observer.py         # per-turn in-memory signals: tool_error / denied / repeat / edit_churn (zero-IO)
  compact_prep.py     # the routine: snapshot+prune → out-of-band sweep → deterministic bound → gated reviewer → writes
  inject.py           # the transient subordinate injection block (in-memory, never persisted)
```
**No model-facing tools** — Stratum exposes none, so the model never proactively invokes a memory
action (this is what fixed the voice leak). It runs only as the compact-prep routine (call-site-gated
in `loop.py`: the trigger at the top of `_compact_context`, the observer seam after the tool loop) and
via the human-invoked `/reconcile` slash command (single-lens, read-only, writes nothing). Injection
is an in-memory tail-segment appended after the frozen prefix (never `ctx.session`, so it can't stack
across `--continue`/`--resume`). Voice exclusion is structural: no tools to leak, and the seams live
only in the coding loop — the voice runner wires none, so a voice ctx never reaches `active()`.

## 8. Deferred (recorded with re-entry gates)

- **Seeding the observed store from existing data** — out of scope. If ever done: migrate any
  room-keyed data from the fact store of record (`facts.jsonl`), not a rendered view; frame any
  reshape as model-assisted + snapshot-gated + human-reviewed (never "deterministic/lossless").
- **Full memory unification** — parked; its unique wins were either wrong for this architecture or
  already present.
- **Native Deep Reconcile** (`/reconcile` via `sub_agent`) — optional later.
- **Voice-lane observation** — needs its own latency-safe seam; not here.

## 9. Source-fact basis (audit against real code)

Compaction: `_compact_context` `loop.py:111`; auto-trigger `loop.py:369` (0.85); warn 0.70
`loop.py:14`; `<4` bail `loop.py:114-116`; `.pre-compact` backup `loop.py:143-147`. Loop seams:
session-start `loop.py:194`; system-message injection `loop.py:217`; tool-turn append
`loop.py:460-471`; text-turn `run_stop` `loop.py:448-450`. Memory: `MemoryManager` / `MemoryWriteTool`
`session/memory.py:29,70`; path `paths.py:28-34`. Registry: last-writer-wins `registry.py:15`; guarded
imports `cli.py:474`. Persona: global/invisible `persona/manager.py:1,46,82`; detached reflection
`reflect_detached.py`. `tmi/`: dormant by default `models.py:235`; cross-room escalator
`escalator.py:56-58,115-136`; pure decay math `weights.py:51-56`. Voice: separate loop
`voice/turn.py` + own `_voice_system()`; dormant-path briefs `voice/turn.py:176-183`. Config: nested
models `config/models.py`; precedence `loader.py:19`.
