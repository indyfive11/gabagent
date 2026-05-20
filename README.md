# gabagent

A Claude Code-style AI coding assistant built on the [Gab AI](https://gab.ai) Developer API.

## Features

- Interactive REPL with streaming responses
- File read/write/edit, grep, glob, bash tools
- Web search (DuckDuckGo) and web fetch (static + JS-rendered via Playwright)
- Cascading model router: fast model for exploration, complex model for code writes
- Session persistence with context compaction
- Plan/approve workflow — reviews plan before executing
- Thinking indicator so you can tell it's working
- MCP server support
- Configurable permissions and hooks

## Install

```bash
pip install gabagent
```

For JS-rendered page fetching (optional):
```bash
playwright install chromium
```

## Setup

```bash
export GABAI_API_KEY=your_key_here
gab
```

Or add `api_key` to `~/.config/gabagent/settings.json`.

## Usage

```bash
gab                          # interactive REPL
gab "list files in src/"     # one-shot
gab --continue               # resume last session
gab --resume <uuid>          # resume specific session
```

## License

GPL-3.0-or-later
