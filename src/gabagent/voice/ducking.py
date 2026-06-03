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
import asyncio
import re
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_DUCK_VOLUME = 18           # percent to duck TIDAL/Mopidy music to while the user speaks
_DUCK_VIDEO_VOLUME = 0.2    # HTML5 <video>.volume (0–1) to duck a browser-played movie to
_SINK_DUCK_PCT = 18         # percent to duck the Mopidy PipeWire sink-input to (system-node backup)


def _state(ctx) -> dict:
    s = getattr(ctx, "_duck_state", None)
    if s is None:
        s = {"tidal_prior": None, "jellyfin_paused": None, "jellyfin_video_volume": None}
        ctx._duck_state = s
    s.setdefault("jellyfin_video_volume", None)  # tolerate state dicts from before this field
    s.setdefault("tidal_sink_prior", None)       # (sink-input index, prior volume %) when ducked
    return s


async def duck_media(ctx, on: bool, session_id: str | None = None) -> dict:
    """Duck (on=True) or restore (on=False) any active media. Fire-and-forget; never raises."""
    ducked = []
    if await _duck_tidal(ctx, on):
        ducked.append("tidal")
    if await _duck_jellyfin(ctx, on):
        ducked.append("jellyfin")
    # Make the duck/restore visible in voice_debug.jsonl so the on/off timing and whether restore
    # actually fired can be joined with the voice-agent's DUCK log. On an `off`, "jellyfin" in `ducked`
    # means the restore succeeded; its absence means nothing was restored (the bug signature).
    try:
        from gabagent.voice.debuglog import dlog
        st = _state(ctx)
        dlog(ctx, "duck", session=session_id, on=bool(on), ducked=ducked,
             jellyfin_saved=st.get("jellyfin_video_volume"), tidal_prior=st.get("tidal_prior"))
    except Exception:
        pass
    return {"ok": True, "ducked": ducked}


async def media_state(ctx) -> dict:
    """Provider-NEUTRAL playback snapshot for the voice client's duck-timing. The shape is generic by
    design — NO brain-specific provider names (jellyfin/tidal/…) cross the brain↔voice protocol, so any
    brain can serve it and the voice side never learns what media the brain controls (see
    [[feedback-gabagent-brain-agnostic-protocol]]). Returns {"playing": bool, "state":
    "playing"|"paused"|"idle"}. Never raises."""
    tidal = await _tidal_state(ctx)
    jellyfin = await _jellyfin_state(ctx)
    playing = tidal == "playing" or jellyfin == "playing"
    state = "playing" if playing else ("paused" if "paused" in (tidal, jellyfin) else "idle")
    # Brain-internal debug only (NOT part of the neutral protocol response): record the per-source
    # breakdown so a "DUCK on SKIPPED (nothing playing)" can be explained — e.g. distinguishing a
    # genuinely paused movie ("paused") from a closed page ("none"). Gated by the voice_debug flag.
    try:
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "media_state", playing=playing, state=state, tidal=tidal, jellyfin=jellyfin)
    except Exception:
        pass
    return {"playing": playing, "state": state}


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


def _debug_on(ctx) -> bool:
    return bool(getattr(ctx, "voice_debug_path", None))


def _tidal_dlog(ctx, **fields) -> None:
    """Best-effort instrumentation for the TIDAL duck/pause interleaving (under investigation: a
    pause while ducked could strand the music low). Logs the read volume/state + saved prior so the
    on→pause→off sequence is fully visible. Gated by the voice_debug flag."""
    try:
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "tidal_duck", **fields)
    except Exception:
        pass


async def _run_pactl(*args, timeout: float = 2.0) -> tuple[int, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            "pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
        return p.returncode or 0, out.decode(errors="replace")
    except Exception:
        return 1, ""


def _parse_mopidy_sink_input(out: str) -> tuple[str, int | None] | None:
    """Find the Mopidy stream's (sink-input index, current volume %) in `pactl list sink-inputs`."""
    for block in out.split("Sink Input #")[1:]:
        if 'application.name = "Mopidy"' not in block and 'node.name = "Mopidy"' not in block:
            continue
        idx = block.splitlines()[0].strip()
        m = re.search(r"Volume:.*?(\d+)%", block)
        if idx.isdigit():
            return idx, (int(m.group(1)) if m else None)
    return None


async def _mopidy_sink_input() -> tuple[str, int | None] | None:
    if not shutil.which("pactl"):
        return None
    rc, out = await _run_pactl("list", "sink-inputs")
    if rc != 0 or not out:
        return None
    return _parse_mopidy_sink_input(out)


def _ambient_cap(ctx) -> int:
    """The % ceiling to hold playing music at continuously (config media_ambient_cap, default 90).
    100 disables the ambient cap. Clamped to a sane range."""
    try:
        c = int(getattr(ctx.config, "media_ambient_cap", 90))
    except Exception:
        c = 90
    return max(1, min(100, c))


async def apply_ambient_cap(ctx) -> None:
    """Cap currently-playing music at the ambient ceiling (lower only, never raise) so VAD can hear the
    user over it. Called when music starts; the speech-duck drops it further and restores to this cap.
    Best-effort on both the Mopidy software mixer and the PipeWire sink-input. Never raises."""
    cap = _ambient_cap(ctx)
    if cap >= 100:
        return
    tc = getattr(ctx.config, "tidal", None)
    try:
        if tc and getattr(tc, "enabled", False):
            from gabagent.commands.providers.tidal import _rpc
            vol = await _rpc(tc, "core.mixer.get_volume", timeout=2.0)
            if vol is not None and int(vol) > cap:
                await _rpc(tc, "core.mixer.set_volume", {"volume": cap}, timeout=2.0)
        found = await _mopidy_sink_input()
        if found and found[1] is not None and found[1] > cap:
            await _run_pactl("set-sink-input-volume", found[0], f"{cap}%")
        _tidal_dlog(ctx, phase="ambient_cap", cap=cap)
    except Exception:
        pass


async def _duck_tidal_sink(ctx, on: bool) -> None:
    """Belt-and-suspenders for the music duck: also attenuate the Mopidy stream at the SYSTEM node
    (PipeWire sink-input). Mopidy's software mixer proved intermittently inaudible (value changes but
    the output stays loud); the sink-input is the node the user reaches by hand, so it's reliably
    heard. Best-effort, paired with the software-mixer duck in _duck_tidal. Never raises."""
    st = _state(ctx)
    try:
        if on:
            if st.get("tidal_sink_prior") is not None:
                return  # already ducked
            found = await _mopidy_sink_input()
            if not found:
                return
            idx, vol = found
            prior = vol if vol is not None else 100
            cap = _ambient_cap(ctx)
            # Don't strand the music low across a brain restart: if it's already at/below the duck
            # level, that's almost certainly a leftover duck, not a real user level — restore to the cap.
            st["tidal_sink_prior"] = (idx, cap if prior <= _SINK_DUCK_PCT else prior)
            await _run_pactl("set-sink-input-volume", idx, f"{_SINK_DUCK_PCT}%")
            _tidal_dlog(ctx, phase="sink_on", sink_idx=idx, sink_prior=vol, ducked_to=_SINK_DUCK_PCT)
            return
        saved = st.get("tidal_sink_prior")
        if not saved:
            return
        idx, prior = saved
        target = min(int(prior), _ambient_cap(ctx))   # restore to the cap (a ceiling), never above it
        cur = await _mopidy_sink_input()      # re-find: the stream/index may have changed
        tgt_idx = cur[0] if cur else idx
        await _run_pactl("set-sink-input-volume", tgt_idx, f"{target}%")
        _tidal_dlog(ctx, phase="sink_off", sink_idx=tgt_idx, restored_to=target)
        st["tidal_sink_prior"] = None
    except Exception:
        pass


async def _duck_tidal(ctx, on: bool) -> bool:
    tc = getattr(ctx.config, "tidal", None)
    if not tc or not getattr(tc, "enabled", False):
        return False
    from gabagent.commands.providers.tidal import _rpc
    st = _state(ctx)
    try:
        if on:
            if st["tidal_prior"] is not None:
                _tidal_dlog(ctx, phase="on_skip", prior=st["tidal_prior"])
                return False  # already ducked — don't clobber the saved level
            state = await _rpc(tc, "core.playback.get_state", timeout=2.0)
            if state != "playing":
                _tidal_dlog(ctx, phase="on_skip", state=state)
                return False
            vol = await _rpc(tc, "core.mixer.get_volume", timeout=2.0)
            if vol is None:
                return False
            st["tidal_prior"] = int(vol)
            await _rpc(tc, "core.mixer.set_volume", {"volume": _DUCK_VOLUME}, timeout=2.0)
            await _duck_tidal_sink(ctx, True)   # also duck the system node (reliably audible)
            _tidal_dlog(ctx, phase="on", state=state, read_vol=int(vol), saved_prior=int(vol),
                        ducked_to=_DUCK_VOLUME)
            return True
        if st["tidal_prior"] is None:
            return False
        target = min(int(st["tidal_prior"]), _ambient_cap(ctx))   # restore to the cap, never above it
        await _rpc(tc, "core.mixer.set_volume", {"volume": target}, timeout=2.0)
        await _duck_tidal_sink(ctx, False)      # restore the system node alongside the mixer
        if _debug_on(ctx):   # the post-restore read-back is only worth its 2 RPCs while investigating
            post_state = await _rpc(tc, "core.playback.get_state", timeout=2.0)
            post_vol = await _rpc(tc, "core.mixer.get_volume", timeout=2.0)
            _tidal_dlog(ctx, phase="off", restored_to=target, post_vol=post_vol, post_state=post_state)
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
            # If the video is already AT or below the duck level, it was almost certainly stranded
            # there by a brain restart mid-duck (the saved prior is in-memory, lost on restart).
            # Saving that as the restore target would keep the movie quiet forever — restore to full.
            st["jellyfin_video_volume"] = 1.0 if float(prior) <= _DUCK_VIDEO_VOLUME else float(prior)
            await _set_video_volume(page, _DUCK_VIDEO_VOLUME)
            return True
        if st["jellyfin_video_volume"] is None:
            return False
        ok = await _set_video_volume(page, float(st["jellyfin_video_volume"]))
        if not ok:
            # The page-eval restore silently failed — KEEP the saved level so the next on:false can
            # retry. Clearing it here (the old behavior) let the next duck read the still-0.2 video and
            # save 0.2 as the "prior", which is how the movie got stuck quiet.
            return False
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
