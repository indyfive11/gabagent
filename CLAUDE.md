# Gab-Agent

A Claude Code-style AI coding assistant built on the Gab AI Developer API.

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
