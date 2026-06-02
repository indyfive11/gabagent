"""Media ducking: quiet/pause whatever media the brain controls while the user is speaking.

The voice client calls POST /media/duck {on:true} on VAD speech-onset and {on:false} on
speech-end. Music (TIDAL via Mopidy) is ducked to a low volume; video (Jellyfin) is paused —
per the voice-agent contract, pause reads better than duck for film dialogue, and it stops the
movie's own audio from being transcribed as user speech. No active media → no-op.

Prior state is kept on ctx so restore returns to the exact level, and is idempotent (a second
duck won't overwrite the saved volume with the already-ducked one).
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_DUCK_VOLUME = 18  # percent to duck music to while the user speaks


def _state(ctx) -> dict:
    s = getattr(ctx, "_duck_state", None)
    if s is None:
        s = {"tidal_prior": None, "jellyfin_paused": None}
        ctx._duck_state = s
    return s


async def duck_media(ctx, on: bool) -> dict:
    """Duck (on=True) or restore (on=False) any active media. Fire-and-forget; never raises."""
    ducked = []
    if await _duck_tidal(ctx, on):
        ducked.append("tidal")
    if await _duck_jellyfin(ctx, on):
        ducked.append("jellyfin")
    return {"ok": True, "ducked": ducked}


async def _duck_tidal(ctx, on: bool) -> bool:
    tc = getattr(ctx.config, "tidal", None)
    if not tc or not getattr(tc, "enabled", False):
        return False
    from gabagent.commands.providers.tidal import _rpc
    st = _state(ctx)
    try:
        if on:
            if st["tidal_prior"] is not None:
                return False  # already ducked — don't clobber the saved level
            if await _rpc(tc, "core.playback.get_state", timeout=2.0) != "playing":
                return False
            vol = await _rpc(tc, "core.mixer.get_volume", timeout=2.0)
            if vol is None:
                return False
            st["tidal_prior"] = int(vol)
            await _rpc(tc, "core.mixer.set_volume", {"volume": _DUCK_VOLUME}, timeout=2.0)
            return True
        if st["tidal_prior"] is None:
            return False
        await _rpc(tc, "core.mixer.set_volume", {"volume": int(st["tidal_prior"])}, timeout=2.0)
        st["tidal_prior"] = None
        return True
    except Exception:
        return False


async def _duck_jellyfin(ctx, on: bool) -> bool:
    jc = getattr(ctx.config, "jellyfin", None)
    if not jc or not getattr(jc, "enabled", True) or not jc.api_key:
        return False
    from gabagent.commands.providers.jellyfin import _sessions, _client
    st = _state(ctx)
    try:
        if on:
            if st["jellyfin_paused"]:
                return False
            sessions = await _sessions(jc)
            playing = next((s for s in sessions if s.get("NowPlayingItem")
                            and not (s.get("PlayState") or {}).get("IsPaused")), None)
            if not playing:
                return False
            async with _client(jc) as c:
                await c.post(f"/Sessions/{playing['Id']}/Playing/Pause")
            st["jellyfin_paused"] = playing["Id"]
            return True
        sid = st["jellyfin_paused"]
        if not sid:
            return False
        async with _client(jc) as c:
            await c.post(f"/Sessions/{sid}/Playing/Unpause")
        st["jellyfin_paused"] = None
        return True
    except Exception:
        return False
