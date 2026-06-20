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
| GET  | `/media/state` | query: `bot_speaking=true\|false` (optional) | `{"playing":bool,"state":"playing"|"paused"|"idle","kind":"audio"|"video"|null}` |

`/media/state` doubles as a ~1 Hz heartbeat. Pass `bot_speaking=true` while the assistant's TTS is actively
playing: it lets the brain keep its duck-watchdog from auto-restoring the bed mid-reply (a long spoken answer
has no incoming user speech to keep the duck alive). Omit it or send `false` otherwise. Optional — a brain that
ignores it still works.

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
| `wake_hold` | `ttl_secs` | Keep the wake/follow-up window open for `ttl_secs` after a media-control turn, so the user can chain commands ("louder", "skip") without re-waking. |
| `convo_hold` | `release` | `release=true` on a terminal reply: restore the bed-duck at TTS-stop instead of holding it the full conversation-hold window. A reply that expects an answer (a question) omits this so the hold stays open. |
| `voice_volume` | `op, value?` | Change the **assistant's own** TTS gain (not media volume). `op` ∈ `up`\|`down`\|`set`; on `set`, `value` is an absolute level `0..1` (1.0 = full, 0.0 = silent). |
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
