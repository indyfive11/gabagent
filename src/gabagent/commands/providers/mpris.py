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
