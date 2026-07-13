# gabagent

A Claude Code-style AI coding assistant built on the [Gab AI](https://gab.ai) Developer API — and,
on the same engine, the pluggable **voice-driven home/media brain** ("Aria") described under
[Voice Mode](#voice-mode).

## Features

- 🎙️ **Voice mode** — run as the brain of a hands-free voice assistant: music (TIDAL), movies (Jellyfin), and KDE desktop control by voice ([see below](#voice-mode))
- Interactive REPL with streaming responses
- File read/write/edit, grep, glob, bash tools
- Web search (DuckDuckGo) and web fetch (static + JS-rendered via Playwright)
- Cross-backend model ladder: a turn escalates from a cheap fast model up to a more capable one as the work gets harder — rungs can span backends (local Ollama → Gab AI → an optional Claude/Anthropic rung). Pin a single model with `/model` or switch the backend with `/backend`.
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

- **Music — TIDAL (via Mopidy):** play tracks, albums, playlists and mixes (by name,
  with shuffle); set an absolute volume; pause/resume. Music auto-ducks (or fully
  mutes) while you speak, and stays ducked through a long spoken reply.
- **Movies — Jellyfin:** search, play on a chosen monitor, pause/resume, leave full
  screen, and adjust volume — in a browser the brain controls.
- **Desktop — KDE/Wayland:** move windows between monitors, close windows by name.
- **Aria's own voice:** "lower your voice" / "speak up" adjusts the assistant's own
  speaking volume — distinct from the music or system volume.

The brain only ducks or controls media playing on **this** machine — playback on
other devices/rooms is left untouched. Irreversible actions require a spoken
confirmation, and it stays honest about what it can and can't do.

**Companion project:** the reference voice front-end (microphone, wake word,
STT/TTS) is [voice-agent](https://github.com/indyfive11/voice-agent). The two are
**loosely coupled** — they only share a small brain-agnostic HTTP+SSE protocol
([docs/VOICE_PROTOCOL.md](docs/VOICE_PROTOCOL.md)), so any compatible front-end can
drive gabagent, and gabagent can be swapped for any brain that speaks the protocol.

## License

GPL-3.0-or-later
