# gabagent

A Claude Code-style AI coding assistant built on the [Gab AI](https://gab.ai) Developer API.

## Features

- 🎙️ **Voice mode** — run as the brain of a hands-free voice assistant: music (TIDAL), movies (Jellyfin), and KDE desktop control by voice ([see below](#voice-mode))
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

## Voice Mode

Run gabagent as the "brain" behind a hands-free voice assistant. A compatible voice
front-end handles the microphone, wake word, and speech-to-text / text-to-speech;
gabagent serves the conversation and the actions over a small local HTTP+SSE protocol:

```bash
gab --voice-serve            # start the brain on 127.0.0.1:8765
```

By voice you can control:

- **Music — TIDAL (via Mopidy):** play tracks, albums, playlists and mixes; set an
  absolute volume; pause/resume. Music auto-ducks (or fully mutes) while you speak.
- **Movies — Jellyfin:** search, play on a chosen monitor, pause/resume, leave full
  screen, and adjust volume — in a browser the brain controls.
- **Desktop — KDE/Wayland:** move windows between monitors, close windows by name.

The brain only ducks or controls media playing on **this** machine — playback on
other devices/rooms is left untouched. Irreversible actions require a spoken
confirmation, and it stays honest about what it can and can't do. The protocol is
brain-agnostic (`/respond`, `/confirm`, `/media/duck`, `/media/state` are all
generic), so any compatible front-end can drive it.

## License

GPL-3.0-or-later
