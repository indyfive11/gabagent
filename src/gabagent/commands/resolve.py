"""Make `run_command` forgiving of plausible-but-wrong command ids — the recovery path a weaker
model (local floor / Aria) leans on when it riffs a name instead of calling list_capabilities.

Two complementary salvages, tried only AFTER an exact + alias miss (see commands/catalog.py):
  1. resolve_media_intent — a GENERIC media-transport guess (`media.playpause`, `media.pause`,
     `jellyfin.pause`, `tidal.toggle`, …) is mapped to whatever is ACTUALLY playing right now
     (Tidal / Jellyfin) via the live media inventory. This is the exact "idiot move" caught live
     2026-06-16: the model issued `media.playpause` (the MPRIS id, not registered) while music
     was on Tidal — the right target was `tidal.pause`.
  2. closest_command_ids — a difflib near-match over the real catalog ids, so an unknown id either
     auto-routes (very close) or comes back as "did you mean: …" in ONE step, not a wasted turn.
"""
from __future__ import annotations
import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Verb (the part after the dot) → normalized transport intent. Covers the shapes models invent:
# media.playpause / media.toggle / jellyfin.pause / tidal.resume / system.skip / …
_MEDIA_VERB = {
    "pause": "pause",
    "play": "resume", "resume": "resume", "unpause": "resume", "continue": "resume",
    "playpause": "toggle", "toggle": "toggle", "play_pause": "toggle",
    "stop": "stop",
    "next": "next", "skip": "next", "forward": "next", "next_track": "next",
    "previous": "previous", "prev": "previous", "back": "previous", "previous_track": "previous",
    # "what's playing" queries the model riffs as media.now_playing / media.current / …
    "now_playing": "now_playing", "nowplaying": "now_playing", "whats_playing": "now_playing",
    "current": "now_playing", "current_track": "now_playing", "current_song": "now_playing",
    # shuffle/randomize → TIDAL tracklist random mode (the model riffs media.shuffle / music.randomize / …)
    "shuffle": "shuffle", "randomize": "shuffle", "random": "shuffle", "shuffle_on": "shuffle",
    "unshuffle": "shuffle_off", "shuffle_off": "shuffle_off",
}

# normalized intent → real command id per provider. Jellyfin routes through jellyfin.control(action).
_TIDAL = {"pause": "tidal.pause", "resume": "tidal.resume", "stop": "tidal.stop",
          "next": "tidal.next", "previous": "tidal.previous"}
_JELLYFIN_ACTION = {"pause": "pause", "resume": "resume", "stop": "stop",
                    "next": "next", "previous": "back"}
# now_playing is a direct per-provider command (a query, not a transport action via control()).
_NOW_PLAYING = {"tidal": "tidal.now_playing", "jellyfin": "jellyfin.now_playing"}


def _looks_like_media_transport(command_id: str) -> str | None:
    """Return the normalized transport intent if `command_id` is a media-transport guess, else None.
    Only fires for media-ish domains so an unrelated unknown id falls through to the difflib salvage."""
    if "." not in command_id:
        return None
    domain, verb = command_id.split(".", 1)
    if domain.lower() not in ("media", "tidal", "jellyfin", "mpris", "music", "player", "playback", "system", "spotify"):
        return None
    return _MEDIA_VERB.get(verb.replace("-", "_").lower())


async def resolve_media_intent(ctx: AgentContext, command_id: str) -> tuple[str, dict] | None:
    """Map a generic media-transport guess to (real_command_id, extra_args) for the actively-playing
    provider, or None if it isn't a transport guess / nothing is playing. Never raises."""
    intent = _looks_like_media_transport(command_id)
    if intent is None:
        return None
    try:
        from gabagent.commands.media import inventory, local_audible
        srcs = local_audible(await inventory(ctx))
    except Exception:
        return None
    if not srcs:
        return None
    active = next((s for s in srcs if s.state == "playing"), None) or srcs[0]
    prov = (active.provider or "").lower()
    if intent == "now_playing":
        rid = _NOW_PLAYING.get(prov)
        return (rid, {}) if rid else None
    if intent in ("shuffle", "shuffle_off"):
        # Shuffle is a Mopidy/TIDAL tracklist play-mode; Jellyfin's web player has no equivalent we drive,
        # so only route it for TIDAL and fall through (→ None) otherwise.
        if prov == "tidal":
            return ("tidal.shuffle", {"mode": "on" if intent == "shuffle" else "off"})
        return None
    if intent == "toggle":
        intent = "pause" if active.state == "playing" else "resume"
    if prov == "tidal":
        rid = _TIDAL.get(intent)
        return (rid, {}) if rid else None
    if prov == "jellyfin":
        act = _JELLYFIN_ACTION.get(intent)
        return ("jellyfin.control", {"action": act}) if act else None
    return None


def closest_command_ids(command_id: str, all_ids: list[str], *, n: int = 3, cutoff: float = 0.6) -> list[str]:
    """difflib near-matches over real catalog ids, best first."""
    return difflib.get_close_matches(command_id, all_ids, n=n, cutoff=cutoff)


def best_match_ratio(command_id: str, candidate: str) -> float:
    return difflib.SequenceMatcher(None, command_id, candidate).ratio()


# Auto-route when the single best near-match is this close; below it, only suggest (don't guess wrong).
AUTO_ROUTE_RATIO = 0.86
