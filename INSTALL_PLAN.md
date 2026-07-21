# Install Plan — Gab-Agent + Voice-Agent Installer

**Purpose:** the single reference for the installer's scope, packaging model, and build order.
**Status (2026-07-12):** packaging model **LOCKED (for now)** after a full adversarial design pass with the
voice-agent side, then a **3-way pressure test** (GA red-team × VAC audit × maintainer) that confirmed the
four forks + layered model and folded in six execution-level corrections (see the change log at the end).
**Nothing is built.** Every phase is gated on explicit approval. The canonical home of the shared core is
now **decided — its own repo** (§10b, resolved 2026-07-20). The four MVP "forks" (§1) are locked
provisionally pending a maintainer re-review.

> **Update 2026-07-20:** `installkit/` is now a standalone public repo (github.com/indyfive11/installkit),
> vendored by both layers at a pinned SHA. The Phase-3 shared surface (A.4 templating + A.5 tokens) has
> landed there at pin **`d3451cb`** (GA-audited, signed, pushed). See §4 and §10b.

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
`installkit/` was born as a neutral-named subtree in gabagent at the MVP, then **promoted to its own repo**
(github.com/indyfive11/installkit, 2026-07-20 — resolves §10b). Both gabagent and voice-agent now **vendor**
it in at a **pinned commit SHA** (current pin **`d3451cb`**; FF-only from here). Consumed by copy, never pip —
`bootstrap.sh` must not need PyPI. **The vendor-pin +
SHA-match CI activate at that Phase-3 vendor point — NOT at the MVP:** the interfaces stay provisional until
voice forces the real shared surface (see the Layer-A note), so freezing them at the MVP would stamp
"proven" on a surface a text install never met. Guards (live from the Phase-3 vendor onward):
- `make vendor-sync` + a CI/precommit **SHA-match check** in both repos (a mismatch fails the check).
- **Import-isolation invariant:** `installkit/` imports only the standard library + what it declares — never
  `from gabagent…` — enforced by a one-line precommit grep, so it always stays vendorable.

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
4. **Addon plugin installers** — Jellyfin (URL + key only; never sets up the server), Tidal/Mopidy (optional
   OAuth, skippable), desktop-control, etc.
5. **Full-stack orchestrator + release** (package reconcile, packaged units, docs).

## 10. Open decisions / status
- **(a) The four MVP forks (§1) are under maintainer re-review.** Locked *provisionally*. The layered model
  survives a fork change; the piece most exposed is the **build order** (MVP = text-first).
- **(b) Canonical home for `installkit/` — DECIDED 2026-07-20: its own repo.** Promoted to
  github.com/indyfive11/installkit; both layers vendor from it at a pinned SHA. Chosen to dissolve the
  authorship deadlock (voice-agent git-operates only its own tree, so shared code inside gabagent couldn't be
  authored there). A.4/A.5 shaped + landed at pin `d3451cb`. *(was: deferred to Phase-3.)*
- **Current directive: stop at scope.** No build until a Phase-1 go.

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
