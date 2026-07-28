# ROADMAP — gabagent

**The single source of truth for what we're building, deferring, and have shipped.**
Supersedes the scattered `PLAN_*` / roadmap docs (archived locally in `docs/archive/`, gitignored).
Keep this living: when a plan changes, change it *here* — don't start a new plan doc.

_Last reconciled: 2026-07-20 (post-v0.7.0 — the close-the-loop gate is now CLOSED)._
_Prior: 2026-07-12 (full founding-vs-shipped audit)._

---

## Charter (see [CLAUDE.md → Project Scope](CLAUDE.md))
**Two products, one repo, one spine:**
1. **gabagent** — a Claude-Code-style terminal coding assistant on the Gab AI API (the founding product and the engine).
2. **Aria** — a voice-driven home/media brain built on that engine.

Both first-class. Shared agent loop, tool registry, config, and Key Invariants. Design rules:
**AI-agnostic** (voice never *requires* the gabagent brain) and **new capabilities are plugins, never spine edits.**

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

## NOW — active
_Between milestones — no gabagent build is in flight. The next active piece is the maintainer's call
(see NEXT). Standing item:_
- **reSpeaker mic-wedge cures** _(voice-agent side)_ — Class A solved; Class B USB-power-cycle rung shipped. Remaining: a live watchdog-driven cycle (maintainer-gated). voice-agent is currently **push-frozen** — this + the confirm-parser stack await the maintainer's direct go.

## NEXT — queued (gate is closed; pick to pull in)
- **Installer — Phase-2 (plugin-installer contract)** — _built + public (`66cae4f`), unreleased (lands in the next tag)._ Each plugin package ships an installer exposing `manifest` + `check`/`install`/`configure`, wired through an **explicit registry** — deliberately *not* auto-discovery.
- **Installer — Phase-3 (voice)** _(voice-agent's build, not ours)_ — 3a + 3b **built**; 3c not started. `installkit/` is its **own repo** (github.com/indyfive11/installkit); A.4 templating + A.5 tokens first landed there at `d3451cb`, and the operative consume pin is now **`78ff1cd`** (post the A.4 boot-safety fix). The shared-artifact-surface question was §10c — **RESOLVED 2026-07-27 = Option 1 (fix-and-consume):** **both repos now vendor the whole `installkit` package byte-for-byte at `78ff1cd`** (the 2026-07-22 pre-promotion-snapshot drift is closed on both sides — gabagent re-vendored the full 6-module package, all byte-identical to the pin), and voice-agent's satellite unit consumes the vendored `render_unit` — A.4's first real consumer. **Committed + public** (gabagent `fa02142` vendor + `b0692a5` content-guard CI; voice-agent side pushed); the live Pi-test remains the maintainer's gate. See `INSTALL_PLAN.md` §10c + its divergence ledger.
- **Installer — §10d voice-host discovery** — **BUILT + public** (unreleased). The full-voice-host role now enables the brain's mDNS advertiser: Layer-B firewall/discovery diagnosis (voice-agent `7217ad5`) + Layer-C voice-host seam (gabagent `aea1306`, `gabagent-install --enable-voice-host --host <ip> [--room-id]`; the ratified graft co-writes a non-loopback `voice_host` and refuses on loopback). **Validated on real hardware 2026-07-27:** the brain's `_voice-brain._tcp` advert is live at its LAN IP (non-loopback, confirmed by an off-box browse); the satellite attaches + authenticates to the brain; and a live spoken-wake→brain-response→TTS cycle completed. (mDNS discovery runs at install time **and** as a boot self-heal: verified from voice-agent source + live hardware, the satellite's `mdns_discover` resolves the brain via mDNS, and a stale pin is corrected to the live brain on boot when the stored host is unreachable — a reachable stored host is kept without a browse, which is the steady state. ARM Pi deploy-test `PROVISION_EXIT 0`.) The remaining self-provisioning piece is the credential/token auto-fetch (3c). See `INSTALL_PLAN.md` §10d.
- **Cross-room arbiter** — brain lane done + committed, **flag-off**. Next: Stage-1 threshold-zoning (voice-side, ~zero code), then enable only if a doorway still double-answers.
- **✦ Builder** — dormant but wired in the CLI. Keep tracked and **surface regularly** ("use the builder").
- **✦ MovieScout LOW-3/4/5** — deferred, but **surface soon & often** (Jellyfin overlay, Trakt, cooldown-on-add).
- **Pre-push content guard** _(offered, not wired)_ — a mechanical private-IP + internal-hostname denylist gate on the tip tree, extending the global pre-push identity hook. Proposed after the v0.7.0 install-specifics scrub; awaiting the go to wire it.

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
- **★ Config env-vars are shadowed by settings.json defaults (`GABAI_*` effectively inert)** (found 2026-07-27, live e2e) — `save_config` writes settings.json with `exclude_unset=False` (every field, incl. defaults), and `load_config` passes the whole file as explicit kwargs to the pydantic `BaseSettings`; **init kwargs outrank env**, so any `GABAI_*` env var whose field has ever been saved is silently overridden by the saved (often default/empty) value. Net effect: an EnvironmentFile-based deploy is a no-op — e.g. a non-loopback voice brain provisioned with `GABAI_VOICE_AUTH_TOKEN` runs **with no effective bearer auth** because the empty saved `voice_auth_token` wins. Security-relevant (a LAN brain intended to require a token doesn't) and it defeats the config-generalization SOP's "env var with a safe default" contract. Fix direction: don't pass unset/default fields as init kwargs (load only the keys actually present/meaningful in the file, or invert precedence so env can override a default). Needs a scoped design pass — it changes precedence fleet-wide. GA lane (`config/loader.py`, `config/models.py`).

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
- **Polish pass** (v0.7.0) — P1 `system.fix_audio` (`944730e`, idempotent "can't hear" recovery — unmute + 50% floor, default-sink only, user-invoked), P2 TIDAL normalized-query cache (`bd3f99f`), P3 eye `error` state (`5c50467`). P4 → DEFERRED (below).
- **Last release:** **v0.7.0 (2026-07-13)** — image gen, movie downloads, MovieScout, headless builder, first installer; closed the close-the-loop gate. (Prior: v0.6.0, 2026-06-20.)

## Open questions the maintainer owns
- ~~Phase-3 `installkit/` canonical home~~ — **RESOLVED 2026-07-20: its own repo** (github.com/indyfive11/installkit), both layers vendor at a pinned SHA.
- Whether **model-change** and **builder** survive long-term.
- Whether to wire the mechanical pre-push content guard (see NEXT).
