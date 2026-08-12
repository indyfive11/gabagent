# Gab-Agent

A Claude Code-style AI coding assistant built on the Gab AI Developer API.

## Project Scope & Charter (living — re-baselined 2026-08-12)

**gabagent is one assistant on one engine, reached through two interfaces.**

The assistant is **Aria**. The engine is the shared agent spine (loop, tool registry, config,
capability plane, memory, persona, the Key Invariants below). The two interfaces are:

1. **Keyboard — the `gab` TUI.** A Claude-Code-style terminal coding assistant on the Gab AI API. The
   founding product (2026-05-19) and the engine everything else is built on.
2. **Voice — the HTTP+SSE brain.** A hands-free home/media brain: music, movies, desktop, timers,
   whole-house/satellite, and a pluggable capability plane.

These are **two doors into the same assistant, not two products.** Aria is Aria whether you type or
speak — shared identity (the global persona layer), tools, and memory; the interface only changes
*how* you reach her.

**Capability spectrum (the design target).** Aria spans a continuum: ambient/media (home brain) →
conversation → **supplemental, bounded coding** ("continue this," small edits, keep a project moving
while I'm away) → **full project handoff** (the headless builder, already shipped). Coding is part of
Aria, not a walled-off product.

**Gating principle — by TASK/CONTEXT, not INTERFACE.** What memory loads and how strict a
confirmation is are decided by *what Aria is doing* (a project/coding task? an irreversible action?),
not by *which door* you came through. Security is **proportional to the task** — a simple media
command should not demand a keyboard confirmation. Bounded elevation (double-voice-confirm or a spoken
override passphrase) is the intended mechanism: **direction, not yet shipped** (see ROADMAP.md).

Design rules (unchanged): **AI-agnostic** (voice never *requires* the gabagent brain — `BRAIN=local/
ollama` stays first-class) and **new capabilities are plugins, never spine edits**.

**Single source of truth for plan & status: [ROADMAP.md](ROADMAP.md).** These founding docs are
*living*: where reality diverged from the original scope, we record it below rather than pretend the
plan never moved.

### Architecture note — one engine, honestly (2026-08-12)

"One engine" is true at the toolbox layer and aspirational at the loop layer, and we say so rather
than overstate it. **Shared today:** one Gab client (`api/client.py`), one flat tool registry
(`tools/registry.py`), one `AgentContext`, one config/session/memory stack, and a genuinely
plugin-shaped capability plane (media/downloads/recommender reached through generic
`run_command`/`list_capabilities` tools — zero new schemas per capability). **Forked today:** the
agent loop itself — `voice/turn.py::_run_turn` *mirrors* `agent/loop.py::run_loop` (~1300 LOC of
parallel loop), with a second system prompt and a second permission surface. **Converging the two
loops (or extracting a shared turn core) is a tracked goal**, not a claim of current state — it is the
seam any context-gated memory or shared security model must reconcile.

### Divergences from founding intent (and why)

Founding intent (README `72f07b6`, 2026-05-19): *"A Claude Code-style AI coding assistant"* — a terminal
coding tool, with no voice, media, or home control, and no "Aria." The five Key Invariants below are
founding and survive **verbatim**. Recorded divergences:

- **2026-06-01 — Voice + media pivot (birth of "Aria").** Added a voice brain (HTTP+SSE) and, the *same
  day*, media control (Jellyfin, TIDAL) via a new command-provider framework. *Justified:* built on the
  existing agent spine as plugins; the coding invariants were untouched; driven by real daily use.
- **2026-06-20 → 2026-07 — Whole-house + breadth.** LAN brain, room addressing, Pi satellite, per-room
  media, cross-room arbiter; then image generation, movie *downloading* (Radarr/Sonarr), a taste
  recommender (MovieScout), self-introspection, and a headless "builder." *Justified but watched:* each
  is a plugin so the core stayed flat — but this is where scope sprawl set in, hence the **close-the-loop
  gate** in ROADMAP.md.
- **2026-08-12 — Charter re-baseline (this rewrite).** The docs had drifted to "two products, one repo,
  both first-class" (a framing first written 2026-07-12, ~8 weeks *after* founding). A Deep Reconcile
  against git + the code surface found the codebase never forked into two products — it is one engine
  that grew capabilities. Re-baselined to **"one assistant, two interfaces,"** which matches the shipped
  code; and re-based the memory/security gating on **context, not interface.**

## Dev Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running

```bash
export GABAI_API_KEY=your_key_here
gab                          # interactive REPL
gab "list files in src/"     # one-shot
gab --continue               # resume last session
gab --resume <uuid>          # resume specific session
```

## Architecture

- `src/gabagent/agent/loop.py` — core async agent loop (the spine)
- `src/gabagent/api/client.py` — Gab AI streaming client with tool call accumulator
- `src/gabagent/tools/registry.py` — tool registration and dispatch
- `src/gabagent/tools/shell_tool.py` — persistent bash subprocess with sentinel I/O
- `src/gabagent/config/` — pydantic-settings config, XDG paths

## Key Invariants

- `AgentContext` is passed to every tool — never use global state
- Rate limiter is checked before every API call
- `edit` tool enforces uniqueness: 0 or >1 matches are hard errors
- Shell state is one persistent bash process per session
- Session JSONL is append-only; compaction writes a new file

## Hardware & Config Generalization (HARD SOP)

This project must run on **anyone's** hardware with **zero code edits**. No hardware-type or
installation-specific value may be a bare constant in code. Every such value MUST be:

1. **A config field / env var with a safe universal default**, where the default is the historical
   no-op behavior — an unconfigured install behaves exactly as before, never worse. Empty/unset must
   be valid and safe (e.g. no GPU override injected when unset).
2. **Detected once by an explicit, inspectable setup/detect step that WRITES the value into config** —
   never a fragile per-startup auto-probe. Per-boot probes misfire on device/startup-order quirks (the
   pipecat `is_format_supported` output probe shipped a 1.84× "chipmunk" TTS bug). Detection uses the
   authoritative OS source: `rocminfo`/`nvidia-smi`/`lspci` (GPU), ALSA/PipeWire (audio rates/devices),
   the primary route interface (LAN IP), `ollama list` (local models).
3. **User-overridable afterward** — the written config stays plain and editable.

Principle: **the running app reads config (dumb); the setup step detects-and-writes (smart); the user
edits config (in control).** A genuinely universal constant (e.g. the 16 kHz pipeline rate tied to STT
model training) may stay hardcoded, but a comment must state WHY it is universal.

Governed values include: GPU env (`HSA_OVERRIDE_GFX_VERSION` — now the `local_env` config map, written by
the Local-backend setup from `rocminfo`), `local_model` name, audio device sample-rate/channels/names,
LAN bind IP, install paths. The sibling voice-agent repo follows the same SOP.

## New-Module Deploy-Safety (HARD SOP — both repos)

A change that adds a module which already-tracked / already-deployed code imports MUST do one of:

1. **Guard the import at the call site** so an absent module degrades to the documented no-op (fail-soft) —
   `try: from newmod import X` / `except ImportError: return None` (or equivalent), with the caller wiring
   nothing when it's absent.
2. **Ship the new module in the same commit / deploy-manifest as its importer.**

**Never an importer without its import target.** Rationale: satellite deploys sync **git-tracked files only**
(`git ls-files` — the Pi's `pi-voice-launch` rsync), so a new *untracked* module + a *tracked* importer =
a guaranteed satellite crash (the Pi `ModuleNotFoundError: image_display` outage, 2026-07-04). Corollary:
under a push-freeze, (1) is the freeze-safe path since (2) is blocked. Extend the guard to cover **construction**,
not just the import — a fail-soft feature that still `ValueError`s while building its object isn't fail-soft.
The sibling voice-agent repo carries the identical SOP.

## Installer Parity (HARD SOP — both repos, mirrored)

**The installer must never lag the code.** A change that adds — or *alters the default of* — an
install-relevant surface MUST update the corresponding installer artifact **in the same commit**. A
capability a fresh `bootstrap.sh` / AUR install can't reach (or reaches with the wrong default) is a silent
regression that only bites a *new* user, never you; green unit tests don't cover it because the gap is in
what the installer *provisions*, not in what the code *does*. The forcing-functions are **automatic** (they
run in the suite + CI, so drift fails the build — not discipline).

**Coverage map — "if your change adds/alters X, the same commit must touch Y":** new user-facing config knob
(a `Field(...)` in `config/models.py`) → installer/`docs/INSTALL.md` wiring, default = historical no-op ·
**flips an existing default** → prove the new default is still the no-op OR document it as a behavior change
(invisible to a "new key" check — call it out) · **renames/removes a knob** → back-compat alias or settings
migration + scrub the stale key · new python dep → `pyproject` **and** the AUR PKGBUILD `depends`/`optdepends`
(check BOTH dep arrays — the v0.8.0 `zeroconf`→optdepends lesson) · new non-python system dep → declare it in
`[tool.gabagent.install].system_pkgs` (so Gate 1 covers it) + PKGBUILD + `docs/INSTALL.md` · new CLI flag/
subcommand → installer/docs teach it · new plugin → registered in `install/registry.py` (else `--addons`
can't reach it) · new module imported by tracked code → track it or fail-soft guard (New-Module Deploy-Safety
SOP) · new systemd unit → installed + boot-safe (Tier-0) · new secret/token file → provisioned `0600` ·
installkit capability bump → re-vendor at the pin.

**Enforcement (automatic):**
1. **`tests/unit/test_installer_parity.py`** (suite + `.github/workflows/installer-parity.yml`):
   - *Gate 1* — packaging parity: every `pyproject` core+voice dep (∪ declared `system_pkgs`) is covered by
     the AUR PKGBUILD `depends`/`optdepends`. Cross-repo (PKGBUILD is in `~/dev/gabagent-aur`), so it **SKIPS
     LOUD** when the clone is absent and is a **HARD release-gate at AUR-bump time** — where the clone is
     present by definition, an absent clone there is a FAIL, not a skip (that skip-on-absent is the exact
     v0.8.0 optdepends-miss hole).
   - *Gate 1b* — `uv lock --check`: the lockfile is in sync with `pyproject`.
   - *Gate 2* — plugin-registry conformance: every `registry.INSTALLERS` entry satisfies the contract + its
     declared `system_pkgs` resolve per-distro. (Conformance/typo check only — NOT proof the plugin is
     satisfied; reachability is the install-smoke's job.)
2. **First-run fail-soft (Gate 4)** — the BEHAVIOR the packaging superset (Gate 1) is blind to. Gate 1 proves
   an optdepend is *declared*; it can't prove a code path that needs an **optional** package degrades when
   it's absent. `anthropic`/`playwright` are pyproject *core* deps demoted to PKGBUILD *optdepends* (pacman
   doesn't auto-install optdepends), so a default install runs without them — an unconditional import
   raw-crashes (the voice-agent dotenv class). Two parts: **`tests/unit/test_firstrun_failsoft.py`** (fast,
   in-suite: importability-aware `anthropic_configured`, ladder-omit, startup degrade) + **`scripts/firstrun_smoke.sh`**
   (authoritative: clean venv with core deps MINUS the optdepend-gated list, asserts every gated seam fails
   soft against a REALLY absent package — an in-tree test can't uninstall a dep it runs under). Keep the
   `OPTDEP_GATED` list explicit: a new optional backend must be added there **and** given a fail-soft guard.
3. **`scripts/installer-parity.sh`** (Gate 3, `make installer-parity` + the `make install-hooks` pre-push
   hook): delta pre-filter with a **canary** (a knob scan that matches nothing is a false green → fail),
   `git merge-base origin/master HEAD` delta base, and a **blocking** untracked-importer check.
4. **Reachability backstop:** in-tree gates prove the artifact was *edited*, not that a fresh install
   *reaches* the feature ("off-box = reachability"). The scripted Headline-PoC install is the standing
   backstop; a new user-facing surface owes a line in it.

**Gated exception:** an internal knob deliberately kept out of the installer is exempt only via an explicit,
diff-visible `.installer-parity-ignore` entry **with a one-line reason** — silence is not exemption. The
sibling voice-agent repo carries a mirrored SOP, `.env.example`-anchored (flat role-provisioner) rather than
contract-anchored; same principle, repo-appropriate detector.

## Testing

```bash
pytest tests/ -v
```

## Config

Settings live at `~/.config/gabagent/settings.json`. Key fields:
- `api_key` — or set `GABAI_API_KEY` env var
- `model` — default `arya`; can be any Gab AI-accessible model
- `base_url` — default `https://gab.ai/v1` (OpenAI-compatible endpoint)
- `permissions.allow` — list of `tool(pattern)` rules to auto-approve
- `permissions.deny` — list of rules to always block
- `hooks` — PreToolUse/PostToolUse/Stop shell commands
- `mcp_servers` — MCP server configurations
- `lsp.servers` — language → LSP command mappings
