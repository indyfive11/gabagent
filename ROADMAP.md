# ROADMAP — gabagent

**The single source of truth for what we're building, deferring, and have shipped.**
Supersedes the scattered `PLAN_*` / roadmap docs (archived locally in `docs/archive/`, gitignored).
Keep this living: when a plan changes, change it *here* — don't start a new plan doc.

_Last reconciled: 2026-07-12 (full founding-vs-shipped audit)._

---

## Charter (see [CLAUDE.md → Project Scope](CLAUDE.md))
**Two products, one repo, one spine:**
1. **gabagent** — a Claude-Code-style terminal coding assistant on the Gab AI API (the founding product and the engine).
2. **Aria** — a voice-driven home/media brain built on that engine.

Both first-class. Shared agent loop, tool registry, config, and Key Invariants. Design rules:
**AI-agnostic** (voice never *requires* the gabagent brain) and **new capabilities are plugins, never spine edits.**

---

## ★ THE GATE — close the loop before opening new domains
**Rule (2026-07-12):** no **new feature domain** starts until the current release loop closes —
1. Land the **installer MVP** (Phase-1: text-only workstation) → see [`INSTALL_PLAN.md`](INSTALL_PLAN.md).
2. **Cut a release** that ships the ~43 commits unreleased since v0.6.0 (2026-06-20).
3. **Then** new domains, each with explicit maintainer sign-off.

_Bugfixes and hardening on already-shipped features are always allowed. Net-new domains are gated._
_Why: live feature requests kept deferring the release gate indefinitely — this is the forcing function._

---

## NOW — active, inside the loop
- **Installer — Phase-1 MVP.** Scope LOCKED (`INSTALL_PLAN.md`); awaiting the build go. Births `installkit/`. The 4 MVP "forks" are under maintainer re-review.
- **reSpeaker mic-wedge cures** _(voice-agent side)_ — Class A solved; Class B USB-power-cycle rung shipped. Remaining: a live watchdog-driven cycle (maintainer-gated).

## NEXT — queued (post-gate, unless explicitly pulled forward)
- **Cross-room arbiter** — brain lane done + committed, **flag-off**. Next: Stage-1 threshold-zoning (voice-side, ~zero code), then enable only if a doorway still double-answers.
- **Polish pass (rescoped 2026-07-12, VAC-vetted)** — the stale `PLAN_polish` collapsed to **4 real items** after ground-truth (9 of its items were already shipped). **Built (uncommitted, unit-green, 1379 tests):** P1 `system.fix_audio` (idempotent "can't hear" recovery — unmute + 50% floor, default-sink only, user-invoked), P2 TIDAL normalized-query cache, P3 eye `error` state. **P1 live-drive** pending a coordinated voice-up window (mutates audio). **P4 → DEFERRED** (below). Ships with the release.
- **✦ Builder** — dormant but wired in the CLI. Keep tracked and **surface regularly** ("use the builder").
- **✦ MovieScout LOW-3/4/5** — deferred, but **surface soon & often** (Jellyfin overlay, Trakt, cooldown-on-add).

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
- **Multi-room** _(unreleased)_ — LAN brain + bearer token, room-id capability handshake, Pi satellite, per-room media, offline local failover, cross-room arbiter (flag-off).
- **Other domains** _(unreleased)_ — image generation, credit-balance guard, self-introspection, self-learning persona, timers + proactive channel, auto-Turbo latency, loop-detector + escalation ladder.
- **Last release:** v0.6.0 (2026-06-20). ~43 commits unreleased since — that backlog is the gate's payload.

## Open questions the maintainer owns
- The 4 installer MVP forks (provenance re-review).
- Phase-3 `installkit/` canonical home (deferred to phase 3, decided on the measured surface).
- Whether **model-change** and **builder** survive long-term.
