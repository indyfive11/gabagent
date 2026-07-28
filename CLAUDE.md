# Gab-Agent

A Claude Code-style AI coding assistant built on the Gab AI Developer API.

## Project Scope & Charter (living — updated 2026-07-12)

**gabagent is two products in one repo, sharing one spine:**

1. **gabagent** — a Claude-Code-style terminal coding assistant on the Gab AI API. The founding
   product (2026-05-19) and the engine everything else is built on.
2. **Aria** — a voice-driven home/media brain built on that engine: an HTTP+SSE "brain" server plus a
   pluggable capability plane (media, desktop, timers, and more).

Both are **first-class**. They share the agent loop, tool registry, config, and the Key Invariants
below. Design rules: **AI-agnostic** (voice never *requires* the gabagent brain — `BRAIN=local/ollama`
stays first-class) and **new capabilities are plugins, never spine edits**.

**Single source of truth for plan & status: [ROADMAP.md](ROADMAP.md).** These founding docs are
*living*: where we diverged from the original scope, we record it below rather than pretend the plan
never moved.

### Divergences from founding intent (and why)

Founding intent (README `72f07b6`, 2026-05-19): *"A Claude Code-style AI coding assistant"* — a terminal
coding tool, with no voice, media, or home control. Recorded divergences:

- **2026-06-01 — Voice + media pivot (birth of "Aria").** Added a voice brain (HTTP+SSE) and, the *same
  day*, media control (Jellyfin, TIDAL) via a new command-provider framework. *Justified:* built on the
  existing agent spine as plugins; the coding invariants were untouched; driven by real daily use.
- **2026-06-20 → now — Whole-house expansion.** LAN brain, room addressing, Pi satellite, per-room
  media, cross-room arbiter. *Justified:* natural extension of the voice product to multiple rooms, still
  plugin-shaped.
- **2026-07 — Breadth grafts.** Image generation, movie *downloading* (Radarr/Sonarr), a taste
  recommender (MovieScout), self-introspection, a headless "builder." *Caveat, not a clean win:* each is a
  plugin so the core stayed clean, but this is where scope sprawl set in — hence the **close-the-loop
  gate** in ROADMAP.md (no new domain until the installer MVP lands and a release ships the backlog).

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
2. **`scripts/installer-parity.sh`** (Gate 3, `make installer-parity` + the `make install-hooks` pre-push
   hook): delta pre-filter with a **canary** (a knob scan that matches nothing is a false green → fail),
   `git merge-base origin/master HEAD` delta base, and a **blocking** untracked-importer check.
3. **Reachability backstop:** in-tree gates prove the artifact was *edited*, not that a fresh install
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
