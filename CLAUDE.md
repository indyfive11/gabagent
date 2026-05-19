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
