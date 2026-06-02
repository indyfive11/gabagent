"""Media ducking: quiet/pause whatever media the brain controls while the user is speaking.

The voice client calls POST /media/duck {on:true} on VAD speech-onset and {on:false} on
speech-end. Both music (TIDAL via Mopidy) and a browser-played Jellyfin movie are ducked by
*lowering volume* — never pausing. Pausing the movie made ducking and a manual "pause" fight each
other (REST-pause vs the page's Space toggle), and volume-duck also keeps working when a
monitor-move switches the OS sink (it sets the HTML5 element's own volume). A native/controllable
Jellyfin client (one we didn't launch in our browser) still pauses via REST, where there's no
toggle conflict. No active media → no-op.

Prior state is kept on ctx so restore returns to the exact level, and is idempotent (a second
duck won't overwrite the saved level with the already-ducked one).
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_DUCK_VOLUME = 18           # percent to duck TIDAL/Mopidy music to while the user speaks
_DUCK_VIDEO_VOLUME = 0.2    # HTML5 <video>.volume (0–1) to duck a browser-played movie to


def _state(ctx) -> dict:
    s = getattr(ctx, "_duck_state", None)
    if s is None:
        s = {"tidal_prior": None, "jellyfin_paused": None, "jellyfin_video_volume": None}
        ctx._duck_state = s
    s.setdefault("jellyfin_video_volume", None)  # tolerate state dicts from before this field
    return s


async def duck_media(ctx, on: bool) -> dict:
    """Duck (on=True) or restore (on=False) any active media. Fire-and-forget; never raises."""
    ducked = []
    if await _duck_tidal(ctx, on):
        ducked.append("tidal")
    if await _duck_jellyfin(ctx, on):
        ducked.append("jellyfin")
    return {"ok": True, "ducked": ducked}


async def media_state(ctx) -> dict:
    """Read-only snapshot for the voice client's duck-timing: {tidal, jellyfin}. Lets it skip
    ducking when nothing is actually playing. Never raises."""
    return {"tidal": await _tidal_state(ctx), "jellyfin": await _jellyfin_state(ctx)}


async def _tidal_state(ctx) -> str:
    tc = getattr(ctx.config, "tidal", None)
    if not tc or not getattr(tc, "enabled", False):
        return "stopped"
    try:
        from gabagent.commands.providers.tidal import _rpc
        st = await _rpc(tc, "core.playback.get_state", timeout=2.0)
        return st if st in ("playing", "paused", "stopped") else "stopped"
    except Exception:
        return "stopped"


async def _jellyfin_state(ctx) -> str:
    jc = getattr(ctx.config, "jellyfin", None)
    if not jc or not getattr(jc, "enabled", True) or not jc.api_key:
        return "none"
    from gabagent.commands.providers.jellyfin import _live_jellyfin_page, _video_paused, _sessions
    page = _live_jellyfin_page(ctx)
    if page is not None:
        return "paused" if (await _video_paused(page)) is True else "playing"
    try:
        for s in await _sessions(jc):
            if s.get("NowPlayingItem"):
                return "paused" if (s.get("PlayState") or {}).get("IsPaused") else "playing"
    except Exception:
        pass
    return "none"


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
    # A movie WE launched plays in a browser page we own → duck its volume (never pause, so it can't
    # fight a manual pause). Anything else (a native client) falls back to REST pause below.
    from gabagent.commands.providers.jellyfin import _live_jellyfin_page
    page = _live_jellyfin_page(ctx)
    if page is not None:
        return await _duck_jellyfin_video(ctx, page, on)
    return await _duck_jellyfin_rest(ctx, jc, on)


async def _duck_jellyfin_video(ctx, page, on: bool) -> bool:
    from gabagent.commands.providers.jellyfin import _video_volume, _set_video_volume
    st = _state(ctx)
    try:
        if on:
            if st["jellyfin_video_volume"] is not None:
                return False  # already ducked — don't clobber the saved level
            prior = await _video_volume(page)
            if prior is None:
                return False
            st["jellyfin_video_volume"] = float(prior)
            await _set_video_volume(page, _DUCK_VIDEO_VOLUME)
            return True
        if st["jellyfin_video_volume"] is None:
            return False
        await _set_video_volume(page, float(st["jellyfin_video_volume"]))
        st["jellyfin_video_volume"] = None
        return True
    except Exception:
        return False


async def _duck_jellyfin_rest(ctx, jc, on: bool) -> bool:
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
