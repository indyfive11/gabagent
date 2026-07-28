# Voice Brain Protocol

The **brain-agnostic** HTTP + Server-Sent-Events contract between a voice front-end (microphone, wake
word, speech-to-text, text-to-speech) and a **brain** (conversation + actions) is owned by the
AI-agnostic front-end project, so the contract isn't defined by any single brain.

gabagent implements it as a reference **brain** (`gab --voice-serve`).

**Canonical spec → https://github.com/indyfive11/voice-agent/blob/main/docs/VOICE_PROTOCOL.md**

Anything that speaks this protocol interoperates — either side is swappable, and nothing
provider-specific ever crosses the boundary.
