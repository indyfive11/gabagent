"""Universal media transport via MPRIS (`playerctl`) — controls ANY running player
(browser, Spotify, mpv, VLC, the Jellyfin web window…). Detected only where playerctl exists.
"""
from __future__ import annotations
import shutil
from typing import TYPE_CHECKING

from gabagent.commands.model import Command, ShellBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def _pc(*args) -> ShellBackend:
    return ShellBackend(argv=["playerctl", *args])


class MprisProvider:
    id = "mpris"

    async def detect(self, ctx: AgentContext) -> bool:
        return shutil.which("playerctl") is not None

    async def sources(self, ctx: AgentContext):
        """Generic LOCAL-audio detection from PipeWire/PulseAudio sink-inputs — the provider-agnostic
        backstop so a browser / mpv / VLC / game stream (no dedicated provider) still registers as audible
        media and triggers the duck. Reads `pactl`, NOT playerctl, so it runs even where detect() is False
        (inventory() calls sources() regardless of detect()). Symmetric with the duck action side, which
        reads the same sink-inputs — so detection and ducking can't disagree (the bug this fixes: a browser
        movie was invisible to /media/state because only jellyfin/tidal contributed sources → playing=False
        → no duck). Excludes Aria's own TTS and the Mopidy stream (tidal already models that source)."""
        try:
            from gabagent.voice.ducking import _run_pactl, _parse_sink_inputs, _TTS_EXCLUDE_PROP
            from gabagent.commands.media import MediaSource
        except Exception:
            return []
        rc, out = await _run_pactl("list", "sink-inputs")
        if rc != 0 or not out:
            return []
        srcs = []
        for si in _parse_sink_inputs(out):
            block = si.get("block", "")
            if _TTS_EXCLUDE_PROP in block:                       # never count Aria's own voice as media
                continue
            if 'application.name = "Mopidy"' in block or 'node.name = "Mopidy"' in block:
                continue                                         # tidal already contributes this source
            # corked is authoritative on PipeWire (Corked:no = playing); State==RUNNING is the PulseAudio
            # fallback. Unknown activity → don't fabricate a source (avoid false-positive ducks).
            corked, state_field = si.get("corked"), si.get("state")
            if corked is False or state_field == "RUNNING":
                state = "playing"
            elif corked is True or state_field in ("IDLE", "SUSPENDED"):
                state = "paused"
            else:
                continue
            # kind defaults to "audio" — a sink-input can't reveal audio-vs-video on its own; `playing` is
            # what gates the duck. (Video best-effort via MPRIS-over-dbus is a later refinement.)
            srcs.append(MediaSource(provider="local", kind="audio", state=state,
                                    locality="local", owned=False))
        return srcs

    def commands(self, ctx: AgentContext) -> list[Command]:
        return [
            Command(id="media.playpause", domain="media", tier=1,
                    summary="Toggle play/pause on the active media player",
                    backend=_pc("play-pause"), examples=["pause", "resume", "play"]),
            Command(id="media.next", domain="media", tier=1,
                    summary="Skip to the next track", backend=_pc("next"), examples=["next track", "skip"]),
            Command(id="media.previous", domain="media", tier=1,
                    summary="Go to the previous track", backend=_pc("previous")),
            Command(id="media.stop", domain="media", tier=1,
                    summary="Stop playback", backend=_pc("stop")),
            Command(id="media.volume_up", domain="media", tier=1,
                    summary="Raise the media player's volume", backend=_pc("volume", "0.1+")),
            Command(id="media.volume_down", domain="media", tier=1,
                    summary="Lower the media player's volume", backend=_pc("volume", "0.1-")),
        ]


PROVIDER = MprisProvider()
