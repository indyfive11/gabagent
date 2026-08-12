# ROADMAP — gabagent

**The single source of truth for what we're building, deferring, and have shipped.**
Supersedes the scattered `PLAN_*` / roadmap docs (archived locally in `docs/archive/`, gitignored).
Keep this living: when a plan changes, change it *here* — don't start a new plan doc.

_Last reconciled: 2026-08-12 (Deep Reconcile — charter re-baseline + release-sync catch-up)._
_Prior: 2026-07-20 (post-v0.7.0, gate CLOSED); 2026-07-12 (founding-vs-shipped audit)._

---

## Charter (see [CLAUDE.md → Project Scope](CLAUDE.md))
**One assistant, one engine, two interfaces (re-baselined 2026-08-12):**
Aria is one assistant on one shared spine, reached two ways —
1. **Keyboard — the `gab` TUI:** a Claude-Code-style terminal coding assistant on the Gab AI API (the founding product and the engine).
2. **Voice — the HTTP+SSE brain:** a hands-free home/media brain.

Two doors into the same assistant, not two products. Shared agent loop\*, tool registry, config, memory,
persona, and Key Invariants. Design rules: **AI-agnostic** (voice never *requires* the gabagent brain),
**capabilities are plugins never spine edits**, and **memory/security gate by TASK/CONTEXT, not interface.**
_\*The turn loop is currently duplicated (`voice/turn.py` mirrors `agent/loop.py`); unifying it is a tracked goal — see CLAUDE.md architecture note._

---

## ★ THE GATE — CLOSED 2026-07-13 (kept as a standing forcing function)
**Rule (2026-07-12):** no **new feature domain** starts until the current release loop closes —
1. Land the **installer MVP** (Phase-1: text-only workstation) → see [`INSTALL_PLAN.md`](INSTALL_PLAN.md).
2. **Cut a release** that ships the commits unreleased since v0.6.0 (2026-06-20).
3. **Then** new domains, each with explicit maintainer sign-off.

**Status: SATISFIED.** Both halves landed — installer Phase-1 MVP built (`74b5d68`, births `installkit/`)
and **v0.7.0 released 2026-07-13** (shipped the full backlog). New feature domains are unblocked again;
the gate did its job. The rule stays on the books as the reusable forcing function for the *next* loop:
whenever the unreleased backlog starts growing again, re-invoke it.

_Bugfixes and hardening on already-shipped features are always allowed. Net-new domains are gated._
_Why: live feature requests kept deferring the release gate indefinitely — this is the forcing function._

---

## ★ ANTI-DRIFT — keep this tracker honest (2026-08-12)
The 2026-08-12 Deep Reconcile found the *scope gate held* but the *docs lagged ~3 releases* (this file
still claimed v0.7.0 as "last release" while v0.8.0/0.8.1/0.8.2 had shipped). Forcing functions, so
reporting-drift fails structurally rather than relying on memory:
1. **Release-sync gate (to build):** a `tests/unit/test_roadmap_sync.py` that fails CI when this file's
   "Last release" ≠ the latest git tag, or a tag has no SHIPPED row. Mirrors the installer-parity gate
   pattern. _[tracked in NEXT]_
2. **One tracker, no orphans:** ROADMAP.md is the single plan root. `.claude/TASKS.md` (an orphan second
   root) is archived to `docs/archive/` (2026-08-12); `INSTALL_PLAN.md`'s vendoring self-contradiction is
   to be reconciled.
3. **Compact-prep reconcile step:** the compact-prep routine diffs ROADMAP "Shipped" vs `git tag` and
   flags any tag missing a row.

---

## NOW — active
_The next active piece is the maintainer's call (see NEXT). In flight:_
- **Stratum — native memory subsystem** — **v1 SHIPPED `167e68b`** (Current Focus window + compact-prep
  routine + observed-habits store; thin additions to the `memory.md` surface, deliberately **not** a
  memory unification; default `enabled=false`). **v2 "reviewed" built but uncommitted + disabled** —
  drops model-facing tools (root-fix for a voice tool-leak), adds a gated-hybrid reviewer + deterministic
  diff-bound + `/reconcile`. Open (maintainer's call): commit v2? re-enable? and **re-gate on CONTEXT not
  interface** per the 2026-08-12 charter re-baseline. Design spec: [docs/STRATUM.md](docs/STRATUM.md).
- **reSpeaker mic-wedge cures** _(voice-agent side)_ — Class A solved; Class B USB-power-cycle rung shipped. Remaining: a live watchdog-driven cycle (maintainer-gated). voice-agent is currently **push-frozen** — this + the confirm-parser stack await the maintainer's direct go.

## NEXT — queued (gate is closed; pick to pull in)
- **Release-sync test gate** _(anti-drift #1, to build)_ — a `tests/unit/test_roadmap_sync.py` asserting "Last release" == latest git tag and every tag has a SHIPPED row; fails CI on drift. See ANTI-DRIFT above.
- **Aria context-gating + voice-security model** _(post charter re-baseline, maintainer-gated design step)_ — re-gate Stratum memory on **context** (project cwd + coding intent), not `voice_mode`; give voice a bounded "project context" it can enter (attach a cwd, gain a bounded coding toolset, load that project's Current Focus); task-proportional confirmation with **double-voice-confirm / spoken override passphrase** (the "stop lobotomizing simple tasks" fix). See CLAUDE.md → Gating principle + Capability spectrum.
- **Loop convergence** _(tracked goal, unscheduled)_ — extract a shared turn core so `voice/turn.py` stops mirroring `agent/loop.py` (~1300 LOC of parallel loop). The seam context-gating + shared security must reconcile. See CLAUDE.md architecture note.
- **Cross-room arbiter** — brain lane done + committed, **flag-off**. Next: Stage-1 threshold-zoning (voice-side, ~zero code), then enable only if a doorway still double-answers.
- **✦ Builder** — dormant but wired in the CLI. Keep tracked and **surface regularly** ("use the builder").
- **✦ MovieScout LOW-3/4/5** — deferred, but **surface soon & often** (Jellyfin overlay, Trakt, cooldown-on-add).
- **Pre-push content guard** _(offered, not wired)_ — a mechanical private-IP + internal-hostname denylist gate on the tip tree, extending the global pre-push identity hook. Proposed after the v0.7.0 install-specifics scrub; awaiting the go to wire it.
- **Machine-agnostic env-var-naming SOP mirror** _(cross-repo sync, 2026-07-28)_ — voice-agent added an "item 4" to its portability hard SOP: an env var / config key MUST NOT encode a host/machine/reference-install name in its identifier; name by ROLE or SUBSYSTEM; a shipped-key rename adds the agnostic name, reads it first, and keeps the old as a deprecated back-compat alias (never a hard break). **Mirror the clause into `gabagent/CLAUDE.md`** to keep the two constitutions in sync. gabagent env-var audit already run — **clean** (no host-named vars; `GABAI_*`/`GAB_*` prefixes are agnostic), so the mirror is the only remaining action. Awaiting the maintainer's go to edit `CLAUDE.md`.

## DEFERRED — tracked, not active
- **Model-change voice feature** (enumerate catalog + switch to a named model) — revived; needs rescope + an implementation decision. May cut later. _Today only the local↔cloud toggle exists._
- **Home-Assistant provider (G1)** — revived; blocked until HA is actually installed.
- **Aria API ⑥ video** — last remaining API item; small build when pulled.
- **Phase-9 self-reflecting router (TMIL)** — research-grade; its Phase-8 dependency is now met.
- **Jellyfin unexpected-device-cast hardening** — diagnosis-gated.
- **Orphaned-satellite hardening** — LOW priority.
- **P4 voice turn-ceiling watchdog** — a ~90s `_run_turn` backstop. Deferred 2026-07-12: a naive wall-clock ceiling would abort turns legitimately suspended awaiting a **spoken confirmation**; a correct version needs a **confirm-aware** budget (pause during confirm-waits) + care around the `done`-emission contract. Thin value (Phase-8 loop-detector + per-op timeouts already cover most hangs) → not worth a risky turn-driver change now.
- **VAC voice-turn eye `error`** — light the shared HAL-eye `error` state on a voice-turn failure (voice-agent owns the eye writer in voice mode; would have paired with P4). Deferred with P4.
- **Asleep + muted self-recovery gap** (found 2026-07-12 during P1 live-drive) — when Aria is asleep AND the default sink is muted, a bare "I can't hear" can't self-recover: the voice-side wake layer eats the utterance (wake-only, dozes off before it reaches the brain) and the user can't hear the sleep feedback either. User must wake+command in one breath ("Hey Aria, wake up — I can't hear"). Wake/sleep edge (voice-side), not a `fix_audio` defect.
- **`system.volume_up` mismap** (found 2026-07-27) — "app X is silent / I can't hear YouTube" resolves to `wpctl set-volume @DEFAULT_AUDIO_SINK@` (master volume) and Aria answers "I've ensured your system volume is up" — misleading when the real cause is a per-app/tab mute (the muted-Vivaldi-tab incident). A silent-app complaint should first inspect **live per-app sink-inputs** and report the muted stream, not raise master volume. GA lane (`commands/providers/system.py`). Minor; needs a scoping pass before any code.
- **`_duck_local_sinks` restart-strand** (found 2026-07-27) — a brain restart *during* a duck/mute window loses the in-memory `ctx._duck_state["local_sink_priors"]`, so unowned browser streams set to `"0%"` are never restored — stranded silent until the user manually re-raises them. Needs the duck priors persisted across restart (or a restart-time restore sweep). GA lane (`voice/ducking.py`). Minor; scope before coding.

## SOMEDAY — research / maybe-never
- **Phase-5.2** — Aria self-improvement by editing her own code.
- **Pipecat Tier-3** (SmallWebRTC / ESP32 / WorkerBus) — held in reserve.

## CUT — recorded, reversible
- **webrtc_noise_gain AGC/NS** (2026-07-12) — abandoned with receipts; superseded by the hardware-AEC mic path.
- **V2 brain `interrupted` SSE** — NO-GO; `/cancel` proved sufficient.

## HANDED OFF
- **pyloudnorm observer exception** (was `REVIEW_LATER_*`) → voice-agent to close.

---

## SHIPPED — condensed (full detail in git history + memory)
- **Coding-assistant core** (v0.2.x) — agent loop, 28 tools, Rich TUI, resumable JSONL sessions, local Ollama, Claude/Anthropic backend, cascading model ladder, plan/approve, MCP / LSP / hooks.
- **Voice brain + command framework** (v0.3.0) — HTTP+SSE protocol, tiered safety/confirmation, addressed-to-me filter, the pluggable provider plane.
- **Media** (v0.3–v0.6) — Jellyfin (search/play/duck/fullscreen/seek), TIDAL via Mopidy, Radarr/Sonarr voice-add, MovieScout recommender.
- **Multi-room** (v0.7.0) — LAN brain + bearer token, room-id capability handshake, satellite, per-room media, offline local failover, cross-room arbiter (flag-off).
- **Other domains** (v0.7.0) — image generation, credit-balance guard, self-introspection, self-learning persona, timers + proactive channel, auto-Turbo latency, loop-detector + escalation ladder, movie downloads (Radarr/Sonarr), MovieScout recommender, headless builder.
- **Installer — Phase-1 MVP** (v0.7.0) — text-only workstation wizard + top-level `installkit/` (Layer-A, stdlib-only) + `bootstrap.sh` + `gabagent-install`. Closed the gate's installer half.
- **Headline self-provisioning thin-client PoC** (2026-07-28, maintainer-signed-off) — a from-scratch voice satellite provisions itself end-to-end: **mDNS discover → over-the-wire operator-approved token pairing → authenticate → live voice turn + real action (music)**, proven on real hardware (Pi satellite → LAN brain). Closes the §10d/3c credential half. See §10d + `INSTALL_PLAN.md`.
- **Config precedence fix** (v0.8.0) — settings now resolve **CLI/override > env (`GABAI_*`) > `settings.json` > default**; previously the whole file was passed as init kwargs (the highest source), so any saved field silently shadowed its env var — an `EnvironmentFile` deploy was a no-op and a LAN brain provisioned with `GABAI_VOICE_AUTH_TOKEN` ran with no effective auth. An empty `GABAI_*` is now treated as unset, and env-only secrets are never written back to the plaintext file. Live-verified on the LAN brain (bearer auth now enforced). Documented in [docs/INSTALL.md](docs/INSTALL.md#configuration). `config/loader.py`, `config/models.py`.
- **Polish pass** (v0.7.0) — P1 `system.fix_audio` (`944730e`, idempotent "can't hear" recovery — unmute + 50% floor, default-sink only, user-invoked), P2 TIDAL normalized-query cache (`bd3f99f`), P3 eye `error` state (`5c50467`). P4 → DEFERRED (below).
- **Installer — self-provisioning** (v0.8.0) — mDNS discovery/advertiser, voice-host role, over-the-wire token pairing (`POST /pair`), the Phase-2 plugin-installer contract (explicit registry), and vendored `installkit` at pin `78ff1cd`. Absorbs the former NEXT items (Phase-2/Phase-3/§10d) that were "built, unreleased"; the headline self-provisioning PoC (Pi satellite → LAN brain, live) landed here.
- **Installer parity — anti-drift** (v0.8.1) — 3 automatic pytest gates + CI + pre-push hook + delta pre-filter + the mirrored Installer-Parity HARD SOP (dev-infra only).
- **First-run fail-soft** (v0.8.2) — optional backend packages (`anthropic`/`playwright`) absent no longer crash a default install: guarded imports + importability-aware backend detection + startup degrade + Gate 4.
- **Stratum memory v1** (HEAD `167e68b`, untagged) — native memory subsystem: Current Focus window + compact-prep routine + observed-habits store; default `enabled=false`. See NOW for the v2/re-gate status.
- **Last release:** **v0.8.2 (2026-07-28)** — first-run fail-soft. (Prior tags: v0.8.1, v0.8.0 self-provisioning installer; v0.7.0 2026-07-13; v0.6.0 2026-06-20.)

## Open questions the maintainer owns
- ~~Phase-3 `installkit/` canonical home~~ — **RESOLVED 2026-07-20: its own repo** (github.com/indyfive11/installkit), both layers vendor at a pinned SHA.
- Whether **model-change** and **builder** survive long-term.
- Whether to wire the mechanical pre-push content guard (see NEXT).
