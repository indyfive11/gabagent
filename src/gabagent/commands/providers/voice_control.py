"""Voice-layer self-controls: commands that adjust the assistant's OWN voice output, not the OS or media.

These only exist in voice mode (detect gates on ctx.voice_mode). The brain can't change its TTS gain
itself — that lives in the voice client — so the backend records the request as a per-turn signal on ctx
that voice/turn.py emits to the voice side as a `voice_volume` SSE event before `done`. The voice client
maps it onto its TTS gain. Wire shape co-designed with the voice agent (F3).

The my-voice-vs-media-volume disambiguation is carried in the command summary so the MODEL routes it:
a reference to HER voice ('lower your voice', 'speak up', 'you're too loud') lands here; a reference to
the music/movie ('turn the music down', 'volume down') stays on the media/system volume path.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from gabagent.commands.model import Command, Slot, PyBackend
from gabagent.api.models import ToolResult

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_REF = "gabagent.commands.providers.voice_control:"


class VoiceControlProvider:
    id = "voice_control"

    async def detect(self, ctx: AgentContext) -> bool:
        # Only meaningful in voice mode — the signal it sets is read by the voice turn loop. In a
        # text/CLI session nothing consumes it, so don't publish it there.
        return bool(getattr(ctx, "voice_mode", False))

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(
                id="voice.set_volume", domain="voice", tier=1, featured=True,
                summary="Change ARIA'S OWN speaking-voice volume — how loud YOU sound, NOT the music or "
                        "movie volume. Use this only when the user refers to your VOICE: 'lower your "
                        "voice', 'speak up', 'you're too loud/quiet', 'talk quieter'. For the music or a "
                        "movie ('turn the music down', 'volume down'), use the media/system volume control "
                        "instead, NOT this.",
                backend=PyBackend(ref=_REF + "set_volume"),
                params=[
                    Slot("op", "enum", True, enum=("up", "down", "set"),
                         description="up = louder, down = quieter, set = an absolute level via value"),
                    Slot("value", "number", False,
                         description="for op=set only: an absolute level from 0 to 1 — 1.0 = full voice, "
                                     "0.0 = silent (e.g. 0.3 = 30%). Only when the user names a level; never to "
                                     "restore a remembered level (the voice side owns persistence)."),
                ],
                examples=["lower your voice (op=down)", "speak up (op=up)",
                          "you're a little too loud (op=down)", "set your voice to 30 percent (op=set, value=0.3)"],
            ),
        ]


async def set_volume(ctx, op: str = "down", value=None) -> ToolResult:
    """Record a my-voice-volume request as a per-turn signal; voice/turn.py emits it to the voice side.
    Never raises — a malformed op falls back to a gentle 'down'. The actual gain change happens on the
    voice client; here we only signal + speak a terse confirm."""
    o = (str(op) or "").strip().lower()
    if o not in ("up", "down", "set"):
        o = "down"
    sig: dict = {"op": o}
    if o == "set" and value is not None:
        try:
            sig["value"] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            pass
    ctx._voice_volume_signal = sig
    confirm = {"up": "Okay, speaking up.", "down": "Okay, lowering my voice.",
               "set": "Okay, voice level set."}[o]
    return ToolResult(output=confirm)


PROVIDER = VoiceControlProvider()
