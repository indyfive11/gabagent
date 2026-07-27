# Install Plan — Gab-Agent + Voice-Agent Installer

**Purpose:** the single reference for the installer's scope, packaging model, and build order.
**Status (reconciled 2026-07-22):** packaging model still **LOCKED (for now)** — the four forks + 3-layer
model survived the 2026-07-12 3-way pressure test and have not been overturned since. Every phase remains
gated on explicit approval.

**What is built (this replaces the former "Nothing is built"):**

| Phase | State | Evidence |
|---|---|---|
| 1 — text-only workstation wizard | **built** | `src/gabagent/install/workstation.py` |
| 2 — plugin-installer contract + reference plugin | **built** | `src/gabagent/install/{contract,aggregate,registry}.py` |
| 3a — audio/GPU detect-and-write, mDNS discovery | **built** (voice-agent) | its `voice_agent_install/{audio_detect,discovery}.py` |
| 3b — satellite role provisioner (`.env` composition) | **built, not yet reachable** | see the divergence ledger below |
| 3c — credential claim handshake | not started | — |
| 4–5 — addon installers, orchestrator + release | not started | — |

**Read the divergence ledger at the end before trusting any section below it.** Three things this document
asserted for a month did not happen, and one of them is a live drift.

> Companion memory: `project-gabagent-installer-roadmap.md` (Tier-1) carries the same model plus the
> internal-topology/process notes that don't belong in a repo doc.

---

## 0. Goal
One coherent install experience that sets up a box to run Gab-Agent (and Voice-Agent if wanted) with
whatever AI backend the user chooses (Gab / Claude / local Ollama). **AI-agnostic** (the voice shell must
never *require* the gabagent brain — `BRAIN=local`/`ollama` stays first-class). The two repos stay
**separate** but **coordinated**.

## 1. Locked decisions — the four forks (provisional, under maintainer re-review)
1. **Mechanism = a guided Python wizard** (interactive, ships in-repo): detect box → ask role + AI
   services + tiers → provision deps → generate config/units/env.
2. **Structure = TWO installers + a shared core** — `gabagent-install` and `voice-agent-install`, sharing a
   small common library (see §2 Layer A). Repo separation + AI-agnosticism preserved.
3. **Scope reach = Core + Voice, then PLUGIN-OWNED addon installers** — each capability plugin ships its own
   installer (§5). Fixes dependency-knowledge drift (the plugin becomes the single source of truth for its deps).
4. **MVP first = Gab-Agent text-only workstation** — detect box → pick AI backend(s) → write config. Smallest
   risk; also *defines* the shared-core interfaces everything else depends on.

## 2. Packaging model — THREE layers (not "shared core + two installers")
Only Layer A is shared. B and C sit *above* it, each in its own installer.

> **★ Interfaces are PROVISIONAL until Phase-3 (2026-07-12 pressure-test ruling).** `installkit/` is *born*
> at the MVP, but a text-only workstation exercises only the wizard primitives + backend picker + partial
> GPU detect — it never touches unit/env templating (A.4), token pairing (A.5), or the GPU *vendor* contract
> (A.3). Those are first shaped in Phase-3 (voice). So the MVP does **not** prove the shared interfaces —
> `installkit/` stays **unfrozen** through Phase-3; the vendor-pin + SHA-match CI (§4) activate only at the
> Phase-3 vendor point, letting voice force the real shared surface. (Sizes below are rough and un-load-bearing.)
>
> **⚠ 2026-07-22 — the vendor point came and went and this never activated.** Phase 3 has shipped; no
> vendor-pin check and no SHA-match CI exist in either repo. Worse, the premise turned out to be wrong in
> the other direction: voice *did* force the shared surface, and the surface **failed to express it** —
> A.4's unit renderer cannot emit `RestartSec` or `WorkingDirectory`, so voice-agent wrote its own renderer
> instead of consuming A.4. See divergence **D2**.

### Layer A — GENUINELY SHARED (`installkit/`) — rough ~430–530 lines, small + stable-*domain*
1. **Wizard PRIMITIVES only** — `prompt()` + a render/panel helper + `save-confirm` (~30 lines). The *control
   flow* is NOT shared: gabagent's is an option-loop (pick a backend), voice-agent's is a linear role
   provisioner. Each keeps its own flow so we don't smuggle a loop shape into a linear installer.
2. **Dep-engine, SYSTEM layer** — `detect_distro()` → apt|pacman, `ensure_system_pkgs()`, guarded
   `ensure_uv()`, cross-plugin dedup, install-or-manual prompt. **Python-runtime provisioning is a
   per-installer CALLBACK** the engine invokes (role + distro passed in): gabagent → its package / venv;
   voice-agent → `uv sync` with role-selected extras. Not a hidden `if app==…`.
3. **Hardware/GPU detection** — one-shot probe (authoritative OS source) that **returns** the detected values
   (a per-repo layer does the config write — never Layer A, or it would need `from gabagent…`); reports GPU
   vendor `amd|nvidia|none` to drive the STT split. **Net-new, NOT "generalized from the existing detector":**
   today's `detect_gpu_env()` returns `{}` for both NVIDIA *and* no-GPU (it only emits the AMD `HSA_OVERRIDE`),
   so it cannot express the vendor triple the STT split needs. Bound by the **hardware-portability SOP**: every
   detected value defaults to *unset = works-as-today*, detection is a setup-writes-once step (never a
   per-startup probe — that shipped the 1.84× chipmunk-TTS regression), and a step that writes a *wrong* value
   is worse than unset.
4. **Templating ENGINE** — render systemd unit / `.desktop` / env-file (mode 0600) from (template + vars).
   Engine shared; the templates themselves live per-repo.
5. **Token-pairing primitive** — mint+persist the voice auth token (brain side) / read-half (satellite side).
   Asymmetric, one-directional consumption.
   *(+ small shared helpers: primary-route IP detect, local-model list, idempotency skip-if-present.)*

### Layer B — VOICE-AGENT-SPECIFIC (`voice-agent-install`, not shared) — ~350–400 lines
Audio device/rate detection (wraps the existing resolver + a structured-return enumerator; voice-agent
produces it — the shared core owns the detection/templating **engines**, not config knowledge: gabagent is
pydantic→`settings.json`, voice-agent is imperative `_env(...)`, ~1734 lines, **no pydantic/schema object at
all** — so there is nothing for Layer A to "own the schema" of; each repo writes its own config, the one
cross-format value (GPU vendor) is written twice) · LAN-brain discovery (net-new: mDNS/port-probe → write
the brain host + token) · role provisioner (full-voice-host / satellite / laptop → its systemd `--user`
units + env files + `.desktop`) · capability-profile write (wake/vad/stt/tts local-vs-remote).

**Realistic Layer-B size ≈ 700–1000+ lines, not ~350–400** (VAC estimate, 2026-07-12): the AEC resolver
reproduction (~300 live lines) + net-new mDNS/port-probe discovery (~150–250) + 3-role provisioner with unit
templates (~200–350) + capability profile + the net-new 0600 env-file emit (nothing writes mode-0600 today).
**Phase-3 kickoff MUST snapshot the reference host + satellite's live `--user` units as the ground-truth templates** — they're
hand-tuned, untracked, and encode hard-won detail (`XDG_RUNTIME_DIR`, linger, `--room-id`, Vulkan device,
`Wants=`/`After=` ordering) a from-scratch provisioner would drift from.

### Layer C — GAB-AGENT-SPECIFIC (`gabagent-install`, not shared) — ~130+ lines
Plugin-discovered addon installers (§5) + the backend-picker steps (Gab / Claude / local).

## 3. Delivery
- **Primary spine = `git clone → ./bootstrap.sh` (uv-based).** This is the one channel that reaches BOTH
  Arch and Debian. (The reference satellite is a **Debian/apt** device — AUR cannot reach it, so an
  AUR-first delivery would miss the actual shipping target.)
- **AUR = thin Arch-only convenience** on top: `gabagent-install` ships inside the existing gabagent
  package; `voice-agent-install` is added to the existing voice-agent AUR package. Both doors lead to the
  same wizard.
- **`bootstrap.sh` is a tiny PER-REPO POSIX script**, *not* vendored Python — it runs *before* uv/Python
  exist (it provisions the interpreter that then runs `installkit/`). It ensures uv safely: use it if
  present, else install via the distro package manager, else print the official install command; auto-install
  only behind an explicit `--yes` (never an unprompted `curl … | sh`).

## 4. Distribution of the shared core — VENDORED at a pinned SHA (not a published package)

**The intended model.** `installkit/` was born as a neutral-named subtree in gabagent at the MVP, then
**promoted to its own repo** (github.com/indyfive11/installkit, 2026-07-20 — resolves §10b), to be vendored
by both layers at a **pinned commit SHA**, FF-only. Consumed by copy, never pip — `bootstrap.sh` must not
need PyPI. The pin + SHA-match CI were to activate at the Phase-3 vendor point, not the MVP, so that
freezing them early would not stamp "proven" on a surface a text install never met. Intended guards:

- `make vendor-sync` + a CI/precommit **SHA-match check** in both repos (a mismatch fails the check).
- **Import-isolation invariant:** `installkit/` imports only the standard library + what it declares — never
  a consuming app — so it always stays vendorable.

**⚠ The actual state, measured 2026-07-22 — vendoring was never carried out. Do not read the paragraph
above as a description of this repo.**

| Claim this doc made | Measured reality |
|---|---|
| Both repos vendor at pin `d3451cb` | **Neither does.** `d3451cb` is vendored nowhere. |
| gabagent vendors the pin | gabagent's `installkit/` subtree is a **pre-promotion snapshot** — 4 modules (`__init__`, `wizard`, `deps`, `hardware`) — matching **no upstream commit**. `templating.py` and `secrets.py` (the whole A.4/A.5 surface) were never copied, and `__init__`'s docstring still says the vendor-pin activates in the *future*. **Scope of the drift, measured:** `deps.py`, `hardware.py` and `wizard.py` are **byte-identical to upstream HEAD**; only `__init__.py` differs (docstring + a two-name-short `__all__`). So this is documentation drift, not code drift — nothing gabagent runs is affected. |
| voice-agent vendors the pin | voice-agent has **no `installkit/` directory and no import of it** anywhere. |
| `make vendor-sync` exists | No such target in either repo. |
| SHA-match CI is live | Does not exist. |

Consequence: **`installkit.templating` (A.4) and `installkit.secrets` (A.5) have zero production consumers
in either repo** — nothing outside installkit's own test suite imports them. They were specced, twice
audited, hardened, and pushed to a public repo for a single intended consumer that then could not use them.
See **D2**. And the sharper form: **the public `installkit` repo has no consumer of any kind** — gabagent's
installer runs against its own in-tree copy, voice-agent has none. Today it is a *publication*, not a
dependency. The only honest reading of "current pin `d3451cb`" is *"the SHA the shared repo happens to be
at"*, not *"the SHA either consumer is running."*

**Standing rule this proves out (CR-3):** a pin asserted as a string in prose is not a pin. Any pin guard
must be **content-derived** — compare a hash of the vendored tree against the upstream object — never a
string compared against a number written in a document. This drift existed for a month precisely because
three documents agreed with each other and nothing compared them to a tree.

*Why not a published package:* it would only buy independent third-party reuse and an independent release
cadence — we have neither, and a hard cross-repo *installed* dependency cuts against "installs separately."
Vendoring keeps one source of truth, eliminates version skew, and needs no publish step.

## 5. Plugin-installer contract (Layer C, gabagent only)
Each capability plugin ships an `install.py` inside its own package implementing:
```
manifest:  system_pkgs, aur_pkgs, python_deps, models, services   # declarative dep list
check()            -> what's already present (dry-run capability report)
install(mode)      -> provision the missing via the core engine    # mode = auto | manual-prompt
configure(detected, secrets) -> write THIS plugin's config
```
Plugins DECLARE; the shared core INSTALLS + DEDUPES + DETECTS. **Discovery is a NEW registry to build, not
"like `_register_tools()`":** runtime tool/provider registration is a hand-edited `try: import … except
ImportError` ladder (one line per plugin) — nothing "lights up automatically" today. Auto-finding each
plugin's `install.py` needs a net-new mechanism (`pkgutil.iter_modules` / entry-points) with no precedent in
the repo. The voice-agent side is flat (no registry) and stays a linear role provisioner — the asymmetry is
intentional, not a gap.

## 6. Role matrix (the installer's first question)
1. **Workstation** — CLI/TUI, text-only, pick AI backend(s). No voice. ← MVP target.
2. **Full voice host** — brain + full voice co-located + optional media/desktop addons. **Must WRITE
   `voice_advertise: true`** when it provisions a LAN-bound brain: the code default is `false` (unset = the
   historical no-op, nothing broadcast), so mDNS discovery is an install-time opt-in the detect-and-write step
   owns — per the hardware/config-generalization SOP. **Layer split:** the role is provisioned by Layer B, but
   `voice_advertise` is a gabagent pydantic `settings.json` field, so per §2's "each repo writes its own
   config" the WRITE is **Layer C's** — Layer B invokes the Layer-C step, never edits `settings.json` itself
   (it cannot `from gabagent…`). Conditional on the gabagent brain: a `BRAIN=local/ollama` voice host has no
   gabagent config and nothing to write.
3. **Satellite** — voice + local STT/TTS, attaches to a remote LAN brain (token-paired), no desktop. (Debian/apt.)
4. **Laptop** — Gab-Agent + optional voice; own brain or thin client.

## 7. Sharp edges (where the real work is — mostly Layer B)
- **Audio / AEC** — per-mic echo-cancel config, or the hardware-AEC branch for an AEC-capable mic. Gnarliest piece.
- **GPU-STT vendor split** — AMD → whisper.cpp (Vulkan); NVIDIA → CTranslate2 (CUDA). Different provisioning;
  resolved once in shared-core detection.
- **Model prefetch** — several models, **multi-GB total (exact size TBD)**, lazy today.
- **Install-slimming** — pin torch to the CPU wheel index so non-NVIDIA boxes don't pull ~3GB of CUDA libs
  that never load (the STT path is whisper.cpp, torch runs CPU-only). Folds into the voice-install phase.
- **Config / unit / env generation** — units, env files, and launchers are hand-made today; the installer
  generates them per role.
- **Secrets + token pairing** — backend API keys, the LAN voice auth token, STT/TTS tokens, optional media OAuth.

## 8. Size picture (rough — NOT load-bearing)
Layer A ~430–530 (shared) · Layer B **~700–1000+** (voice, revised up 2026-07-12) · Layer C ~130+ (gabagent,
likely low — the existing backend picker is already 212 lines) · **plus the per-plugin `install.py` files**
(the most config-coupled surface, deferred to Phase-4, NOT counted here). **The old "shared ≈ ⅔" claim is
DROPPED** — with Layer B revised up and plugins excluded, the shared fraction is not ⅔ and was never measured.
Vendoring is justified on its **real** legs (§4/§7), not on a line-count: no third-party reuse, no independent
release cadence, the "installs separately but coordinated" rule, and the Debian satellite (AUR can't reach it).

## 9. Build order (each phase gated on approval)
1. **MVP — Gab-Agent text-only workstation wizard** → *births `installkit/`* with **provisional** interfaces
   (it validates delivery plumbing + the backend picker, NOT the shared surface — see the Layer-A note). Least
   risk, no voice coupling yet.
2. **Plugin-installer contract** — formalize the manifest + `check/install/configure` + the **net-new** plugin
   registry (§5); retrofit one reference plugin.
3. **`voice-agent-install`** — the hard part (§7): audio/AEC, GPU-STT vendor split, model prefetch + torch
   slimming, unit generation, satellite token-pairing. **This phase forces A.3/A.4/A.5 into their real shape;
   snapshot the reference host + satellite live units as templates FIRST. Voice-agent vendors `installkit/` here — vendor-pin +
   SHA-match CI go live at this point, not before.**
   > **⚠ Reconciled 2026-07-22.** 3a and 3b shipped; the snapshot requirement *was* honoured (voice-agent's
   > `units.py` header records the live satellite unit and it immediately earned its keep — the running unit
   > turned out to be transient, i.e. no persistent unit exists on any satellite today). But the vendoring
   > clause did **not** happen (**D1**), A.4 could not express the unit this phase needed (**D2**), and 3b's
   > provisioner is not reachable from any entry point (**D3**). 3c is not started.
4. **Addon plugin installers** — Jellyfin (URL + key only; never sets up the server), Tidal/Mopidy (optional
   OAuth, skippable), desktop-control, etc.
5. **Full-stack orchestrator + release** (package reconcile, packaged units, docs).

## 10. Open decisions / status
- **(a) The four MVP forks (§1) are under maintainer re-review.** Locked *provisionally*. The layered model
  survives a fork change; the piece most exposed is the **build order** (MVP = text-first).
- **(b) Canonical home for `installkit/` — DECIDED 2026-07-20: its own repo.** Promoted to
  github.com/indyfive11/installkit to dissolve the authorship deadlock (voice-agent git-operates only its own
  tree, so shared code inside gabagent couldn't be authored there). A.4/A.5 were shaped and landed there at
  `d3451cb`. *(was: deferred to Phase-3.)* **The repo decision stands; the vendoring that was supposed to
  follow it did not happen — see §4 and D1.**
- **(c) DECIDED 2026-07-27 (maintainer) = Option 1, fix-and-consume.** A.4 is now fixed (`9b389a4`
  RestartSec-chokepoint + WorkingDirectory, `0b8ae18` docs); voice-agent's satellite unit will **consume
  `installkit.templating.render_unit`** instead of its own `units.py` renderer — making A.4 its first real
  consumer and proving the shared boot-safety surface. Scoped + 2-agent pressure-tested (both trees) and
  ratified in GA↔VAC collab (full consensus 2026-07-27). Findings that shaped the build:
  1. **No boot-safety regression** — installkit is equal-or-stronger on all six invariants `units.py`
     encodes (`Requires=`/sub-second-restart are *unexpressible*); the two rendered-text diffs
     (`TimeoutStartSec=15`, one-per-line Wants/After) are systemd-inert.
  2. **Vendor the WHOLE `installkit/` package (all 6 modules) at content-derived pin `78ff1cd`** as tracked
     top-level `*.py` — NOT the old prose pin `d3451cb` (predates the A.4 fix → would ship a weaker renderer
     green). Whole-package (not a 2-file subset) is required so the SHA-match stays a clean whole-tree
     compare and the pinned `__init__` isn't forced into a lie; all 6 modules are stdlib-only so nothing new
     reaches the Pi, and 3c consumes `installkit.secrets` next anyway.
  3. **Deploy-safety / separability guards:** vendor top-level so the `git ls-files '*.py'` Pi rsync carries
     it; add an `installkit.__file__`-inside-the-voice-agent-tree test (blocks a sibling-`~/dev/installkit`
     `path=` dep from splitting host↔Pi resolution) + a mirror stdlib-only import-isolation test over the
     copy. voice-agent never imports gabagent; installkit stays app-agnostic → products stay separable.
  4. **Dead-code repoint:** delete `units.py`'s `render_unit`/`UnitSpec`; repoint its tests through the
     vendored renderer (assert the newline case on `ValueError` *type*, not message).

  This resolves D1 (the vendor mechanism gets its first real consumer) and D2 (A.4 now expresses its unit).

  **Ordering:** decision (done) → build + unit-test + Pi-deploy-test (freeze-safe, offline-complete) →
  *new* public pin + SHA-match CI. installkit is public and FF-only, so the pin-publish + CI wiring
  **cannot ship while the push freeze is on** — the vendored copy records `78ff1cd` as
  *provisional-until-pushed*; finalizing is gated on the maintainer lifting the freeze. Each commit/push its own go.
- **(d) Firewall / discovery reachability check — DESIGN RATIFIED 2026-07-21 (GA↔VAC consensus), authoring
  gated.** A brain host with a default-drop input policy silently drops inbound mDNS (5353/udp). Because mDNS
  is query/response, discovery then returns nothing and the symptom is **indistinguishable from a broken
  advertiser** — the failure looks like the wrong layer. Measured on a live default-drop host: firewall
  closed → `12.2s → None`; the same host with `224.0.0.251:5353/udp` allowed → `0.27s → BrainEndpoint`. This
  is a PoC-blocker class, not a single-box quirk: any locked-down brain host hits it.
  - **Discriminator (how the check tells the two apart):** a satellite catches the host's unsolicited
    *announce burst* (sent on service start) but sees nothing when it merely *queries*. So a check that
    browses and gets silence, when the advertiser is known-good, is diagnosing a **filtered responder**, not
    a dead one.
  - **Where it lives — the satellite-install path only.** An off-box reachability check needs a *second* box
    to run the query, and at voice-host install time the satellite may not exist yet — so the brain host
    **structurally cannot run this check on itself.** It belongs on the satellite installer, after the brain
    host + port are known.
  - **Layer split (ratified, no new shared surface):** **Layer B (voice-agent)** owns the browse, the reason
    vocabulary, and the rendered operator remedy · **Layer A (installkit)** gets **nothing new** · **Layer C
    (gabagent)** does one thing: the voice-host role enables the advertiser (`voice_advertise: true`, per §6
    role 2). That is its entire involvement here.
  - **Rules the check obeys:** (1) **detect-and-report, never mutate** — the installer prints the gap and the
    remedy, it never edits a host firewall; (2) **effect-check, not rule-parse** — verify by attempting the
    browse and observing the result, never by parsing nftables/iptables/firewalld rules (not portably
    introspectable, fail-open); (3) **non-fatal** — a filtered/absent responder does not abort the install;
    the satellite falls back to the typed `host + port` path (§10c pairing floor) and prints the gap.
    Auto-discovery is a **parallel enhancement (CR-4)**, never a hard dependency.
  - **Reason vocabulary the browse reports** (drives the operator remedy text): `no-adverts` (nothing seen —
    likely filtered or advertiser off), `filtered=N` (adverts seen for other services but not the brain),
    `unconfirmed` (advert seen but the resolved endpoint didn't answer a probe), `loopback-advert` (advert
    resolves to a loopback address — misconfigured bind).
  - **Build state (measured, do not read the design above as shipped):** the browse *primitive* exists and is
    tested (`voice_agent_install/discovery.py`) but is **UNWIRED** — `discover_brain`/`default_providers`
    have **no production caller** outside the boot re-resolve path; the reason-vocabulary reporting and the
    operator remedy rendering are **unbuilt**. This section specifies work to do, not work done.
  - **Corollary carried from the mDNS close-out:** a fail-soft `except` around discovery converts a hard
    failure into a silent absence — so any discovery path owes an **out-of-process, off-box effect check**;
    `discovery.py`'s bare `except Exception` (not `except ImportError`) means an *installed-but-broken*
    zeroconf renders identically to "no adverts," which this check must not mask.
- **Current directive:** Phases 1–3b are built; every further build, commit, push, and pin move is
  individually gated.

---

## Divergence ledger — where reality left the plan (dated, 2026-07-22)

Recorded rather than quietly corrected, so the next reader can see *what* moved and *why*. Each entry is
grounded in a command run against the real trees, not in another document.

**D1 — Vendoring never happened (§4, §9.3).** The plan said both repos would vendor `installkit/` at pin
`d3451cb` with a `make vendor-sync` target and a SHA-match CI gate. None of it was built. gabagent carries a
pre-promotion snapshot matching no upstream commit; voice-agent carries nothing; the public repo has no
consumer of any kind. *Why it went unnoticed:* the pin was asserted as a string in three documents that
agreed with each other, and nothing ever compared a document to a tree.
*Bounded on measurement, not assumed:* every module gabagent actually imports is byte-identical to upstream
HEAD — the divergence is one `__init__.py` (docstring + `__all__`) plus the two modules never copied. So no
code reconciliation is pending, and retiring the vendor model would cost one file and three documents, not a
migration. Worth stating the inverse too: in the one place a shared surface was genuinely exercised, both
sides converged on identical code; only the **unexercised** surface drifted.
**Justified? No — this is drift, not a decision.** It is the open question in §10c.

**D2 — The shared unit renderer could not express its only consumer (§2 Layer-A, §4).** Phase-3 was supposed
to force A.4 into its real shape. It did the opposite: `installkit.templating.render_unit` has no
`WorkingDirectory` parameter and never emits `RestartSec`, so voice-agent wrote its own renderer. The
`RestartSec` gap is the serious one — with none emitted, the value falls through to the service manager's
own `DefaultRestartUSec` (100 ms as shipped), which is per-box configurable state the renderer cannot see and
did not write. A unit that fails at boot then exhausts its start limit in a fraction of a second and latches
`failed`, requiring a manual `reset-failed` — the remote-hands dependency this project exists to remove. The
realistic trigger is not an exotic fault but the cold-boot sound-server race already in the tracker.
*Two lessons worth keeping:* (i) a boot-safety chokepoint that cannot express a safety-critical parameter
does not omit it — it silently sources it from the machine, which is the config-generalization rule broken
inside the safety mechanism; (ii) both renderers latch on a *persistent* fault, so the real difference
between them is transient-tolerance (sub-second vs over a minute), not brick-vs-no-brick.
**Justified? Partly** — writing a working renderer rather than blocking on a shared one was the right call in
the moment; leaving the plan asserting the opposite was not.

**D3 — Built ≠ reachable (§9.3).** 3b's provisioner composes and validates a `.env` and renders a unit
string, and 26 tests pass over it — but nothing writes the unit, runs `systemctl --user enable`, or enables
linger, and until the entry point below existed nothing invoked any of it. A tested seam with no production
caller is unbuilt work that looks shipped, and a test suite measuring a library cannot observe that nothing
calls it. **Standing check, not a lesson:** before claiming a component built, grep for a caller outside
`tests/` — the exact dual of the deploy-safety rule (never an importer without its target; equally, never a
target without its importer).

**D4 — The install entry point was an empty slot (§3).** §3 named `git clone → ./bootstrap.sh` as the primary
spine from the start, but no `bootstrap.sh` was ever tracked in either repo, so the steps between `git clone`
and the provisioner — create a venv, know the module name, know the argument shape — were undocumented and
untested, which is exactly the per-box-varying part. Now decided (2026-07-22): **a tracked `bootstrap.sh` at
the voice-agent repo root is THE canonical entry point**, and the other shapes are *derived* from it, not
rivals — the AUR package is a thin wrapper over the same path, and a satellite's rsync tree is a **deploy
mechanism, not an install target**. Both install *layouts* remain supported (repo checkout and the XDG data
directory), resolved from the running module's own location; the entry point decides where a *fresh* install
lands, it does not narrow what an *existing* install may be. *Corollary for any entry point:* a handoff that
execs a module with no `__main__` exits 0 having done nothing — success and silent no-op are indistinguishable
by exit status, so an install must assert its **artifacts**, never `$?`.

**D5 — The self-provisioning headline is not reachable from the satellite role alone (§0, §6).** The satellite
role is the only validated profile that exists; the voice-host role that would emit the service map is
unbuilt. Until it (or the 3c claim handshake) lands, a satellite install hand-prompts credentials that a
human must first fetch off the brain box. **Justified as sequencing, but it was never written down** — the
plan implied an end-to-end story the built roles cannot deliver.

---

## Change log — 2026-07-12 3-way pressure test (GA red-team × VAC audit × maintainer)
The 4 forks + 3-layer model were **confirmed** (not overturned). Six execution-level corrections folded in:
1. **`installkit/` interfaces are PROVISIONAL until Phase-3** — the text MVP touches only the easy ⅓; A.3/A.4/A.5
   are first shaped by voice. Vendor-pin + SHA-match CI now activate at the Phase-3 vendor point, not the MVP
   (§2 Layer-A note, §4, §9). Removes the false "MVP proves the interfaces" signal.
2. **A.3 GPU vendor detect is NET-NEW** (existing `detect_gpu_env()` can't express `amd|nvidia|none`); it
   *returns* values, a per-repo layer writes; bound by the hardware-portability SOP (unset=no-op, write-once,
   no per-startup probe) (§2 A.3).
3. **"Shared core owns the schema" reworded** — voice-agent config is imperative `_env(...)` with no schema
   object; Layer A shares detection/templating *engines*, config knowledge is per-repo (§2 Layer-B).
4. **Layer-B size revised ~350–400 → ~700–1000+**; the **"shared ≈ ⅔" claim DROPPED** — vendoring rests on its
   real legs, not a line-count (§2 Layer-B, §8).
5. **Plugin discovery is a NEW registry to build**, not "like `_register_tools()`" (a hand-edited import ladder
   with no auto-discovery today) (§5).
6. **Phase-3 must snapshot the reference host + satellite live systemd units as ground-truth templates** before authoring the
   provisioner (§2 Layer-B, §9).
Minor: bootstrap is uv-based while gabagent's documented dev path is venv+pip — a second, currently-undocumented
install path (fine, note for docs). **Verdict: all three measures agree — sound plan, ship with the above.
Awaiting the maintainer's Phase-1 build go.**
