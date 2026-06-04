# Voice brain protocol

A small, **brain-agnostic** HTTP + Server-Sent-Events contract between a **voice front-end**
(microphone, wake word, speech-to-text, text-to-speech) and a **brain** (conversation + actions).

gabagent is a reference **brain** (`gab --voice-serve`); [voice-agent](https://github.com/indyfive11/voice-agent)
is a reference **front-end**. Either side is swappable — anything that speaks this protocol interoperates, so
nothing provider-specific (TIDAL/Jellyfin/…) ever crosses the boundary.

The brain binds **loopback only** (`127.0.0.1:8765` by default). The front-end connects (or spawns the brain),
sends transcribed user utterances, and renders the streamed response as speech.

## Endpoints

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET  | `/health` | — | `{"status":"ok","mode":"voice"}` |
| POST | `/respond` | `{session_id, text}` | **SSE** stream of events (below). `409` if a turn is already in progress for the session. |
| POST | `/confirm` | `{session_id, id, approved, passphrase?}` | **SSE** continuation stream. `404` unknown session, `409` nothing awaiting confirmation / no match. |
| POST | `/cancel` | `{session_id}` | `{"ok":true}` — aborts the in-flight turn (barge-in). |
| POST | `/media/duck` | `{session_id, on, mute?}` | `{"ok":true,"ducked":[…]}` — quiet/restore local media while the user speaks (`ducked` is opaque, brain-internal). |
| GET  | `/media/state` | — | `{"playing":bool,"state":"playing"|"paused"|"idle","kind":"audio"|"video"|null}` |

## SSE events

Each event is a single `data: {json}\n\n` frame on the `/respond` and `/confirm` streams. Empty/zero fields are
omitted. `type` is always present.

| `type` | Fields | Meaning |
|--------|--------|---------|
| `token`   | `text` | A chunk of speakable response text (stream these to TTS). |
| `status`  | `text` | A short status line (e.g. "Trying tidal…") — optional to speak. |
| `confirm` | `id, tier, method, summary, reason?, prompt_is_complete?` | A gate confirmation. `method` ∈ `spoken_yesno`\|`keyboard`\|`passphrase`. By convention `summary` is a bare action and the front-end appends the yes/no; if `prompt_is_complete` is true, speak `summary` verbatim. Reply via `POST /confirm`. |
| `blocked` | `action, reason` | An action was refused by policy. |
| `error`   | `text, summary` | Turn-level failure — `text` is speakable, `summary` is the structured cause. |
| `done`    | — | Terminal: the turn (or confirm continuation) is complete. |

## Design principles

- **Brain-agnostic / provider-neutral.** No provider names cross the boundary. `/media/state` is generic —
  `kind` is a media *type* (`audio`/`video`), never a provider. The brain owns the *decision* (which provider,
  duck vs pause) and the *action*; the front-end owns audio I/O and timing.
- **Locality.** The brain only auto-ducks/controls media on **this** machine (`/media/duck` and the
  `/media/state` snapshot are scoped to local audio); playback on other devices is never touched automatically.
- **`mute`.** `/media/duck {on:true, mute:true}` deepens the duck to a full mute (volume 0) for an open
  wake/command window, so a music vocal can't bleed into the transcription; `mute` defaults false → a plain
  gentle duck.
- **Two-phase confirm.** A `confirm` event pauses the turn; the front-end collects the user's yes/no and posts
  `/confirm`, and the continuation streams back on *that* response.
- **Loopback only.** The server binds `127.0.0.1`; it is not a network service.

## Building your own side

- **A different front-end** (other wake word / STT / TTS): connect to the brain, POST `/respond` with the
  transcript, render `token` events as speech, handle `confirm`/`error`/`done`, and call `/media/duck` on
  speech onset/end. That's the whole integration.
- **A different brain** (other LLM / assistant): serve these endpoints. As long as `/media/state` stays
  provider-neutral and the SSE event types match, an existing front-end drives it unchanged.
