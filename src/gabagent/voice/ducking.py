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
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_DUCK_VOLUME = 18           # percent to duck TIDAL/Mopidy music to while the user speaks
_DUCK_VIDEO_VOLUME = 0.2    # HTML5 <video>.volume (0–1) to duck a browser-played movie to
_SINK_DUCK_PCT = 18         # percent to duck the Mopidy PipeWire sink-input to (system-node backup)

# Universal local-media duck: when a wake/command window opens (mute=True), drop EVERY local media
# sink-input to 0 so Rob is "heard over ANY media playing" (anti-bleed for the open window — complementary
# to the voice-gate's wake authority, which stays). Two streams are EXCLUDED: the assistant's own TTS output
# (never mute Aria) and the owned Mopidy stream (ducked separately by _duck_tidal_sink, so it's not double-
# managed). TTS is matched by a stable property VAC stamps on its output stream, with a node-name safety net.
_TTS_EXCLUDE_PROP = 'gabagent.duck_exclude = "1"'   # VAC stamps this on the TTS sink-input

# After a new track starts, its PipeWire sink-input can appear a beat late; poll this many times (× delay)
# for it before giving up, so apply_ambient_cap can un-strand it (F1). Tuned to ~1.6s worst case.
_AMBIENT_SETTLE_TRIES = 8
_AMBIENT_SETTLE_DELAY = 0.2


def _state(ctx) -> dict:
    s = getattr(ctx, "_duck_state", None)
    if s is None:
        s = {"tidal_prior": None, "jellyfin_paused": None, "jellyfin_video_volume": None}
        ctx._duck_state = s
    s.setdefault("jellyfin_video_volume", None)  # tolerate state dicts from before this field; <video> fallback
    s.setdefault("jellyfin_sink_prior", None)    # (browser sink-input idx, prior vol %) when the movie is ducked
    s.setdefault("tidal_sink_prior", None)       # (sink-input index, prior volume %) when ducked
    s.setdefault("muted", False)                 # the active duck has been DEEPENED to a full mute (vol 0)
    s.setdefault("local_sink_priors", None)      # {sink-input idx: prior %} for the universal local-duck
    # Duck-WINDOW lifecycle: the authoritative "is a speech-duck currently on" flag (set on duck on, cleared
    # on duck off), independent of the prior tuples. The mid-duck reconcile keys its level decision off this
    # so it can't strand a new track at the duck level when the window has actually closed (the reconcile-vs-
    # off race). seq + opened_mono let us log each window's duration to spot a window stuck open (missing off).
    s.setdefault("duck_window_open", False)
    s.setdefault("duck_window_seq", 0)
    s.setdefault("duck_window_opened_mono", None)
    # Duck-watchdog heartbeat: monotonic time of the last duck-on OR incoming voice activity. The watchdog
    # (checked on the ~1 Hz /media/state poll) auto-restores a duck left open with no refresh past the
    # configured grace — the brain's safety net for a voice-side on-without-off (the movie-announcement strand).
    s.setdefault("last_duck_refresh_mono", None)
    return s


async def duck_media(ctx, on: bool, session_id: str | None = None, mute: bool = False) -> dict:
    """Duck (on=True) or restore (on=False) any active media. Fire-and-forget; never raises.

    `mute=True` (only meaningful with on=True) DEEPENS the duck to a full mute (vol 0) instead of the gentle
    ambient duck — used while a wake/command window is open so the media's acoustic AEC residual can't leak a
    music vocal into a spurious USER turn. It can deepen a duck that's already active (the VAD-onset 18%) to 0
    without clobbering the saved pre-duck level, so restore on `on=False` still returns to the real volume.
    Defaults False → byte-for-byte the pre-mute behavior for any client that omits the flag."""
    from gabagent.voice.debuglog import dlog
    # `duck_begin` paired with the `duck` end event: a duck_begin with no duck means a provider duck hung
    # (e.g. a page-eval on a dead movie page). dlog is self-guarding, so these never raise.
    _t0 = time.monotonic()
    dlog(ctx, "duck_begin", session=session_id, on=bool(on), mute=bool(mute))
    # Track the duck-window lifecycle so the reconcile knows whether a duck is genuinely open and so a
    # window's open-duration is visible (a 30s+ window = a missing off, the real strand trigger).
    st0 = _state(ctx)
    win_ms = None
    if on:
        st0["last_duck_refresh_mono"] = _t0       # every duck-on refreshes the watchdog
        if not st0.get("duck_window_open"):
            st0["duck_window_seq"] = st0.get("duck_window_seq", 0) + 1
            st0["duck_window_opened_mono"] = _t0
            st0["duck_window_open"] = True
    else:
        if st0.get("duck_window_opened_mono") is not None:
            win_ms = int((_t0 - st0["duck_window_opened_mono"]) * 1000)
        st0["duck_window_open"] = False
    ducked = []
    if await _duck_tidal(ctx, on, mute):
        ducked.append("tidal")
    if await _duck_jellyfin(ctx, on, mute):
        ducked.append("jellyfin")
    # Universal local-media duck: on a full-mute window-open, silence every OTHER local media stream too
    # (unowned tabs/apps), restoring on close — so Rob is heard over ANY local media. No-op on a plain duck.
    if await _duck_local_sinks(ctx, on, mute):
        ducked.append("local")
    # A restore clears the mute flag once, centrally (not per-provider) so it resets even in a tidal-only or
    # jellyfin-only setup where one provider's off-branch never runs.
    if not on:
        _state(ctx)["muted"] = False
    # Make the duck/restore visible in voice_debug.jsonl so the on/off/mute timing and whether restore
    # actually fired can be joined with the voice-agent's DUCK log. On an `off`, "jellyfin" in `ducked`
    # means the restore succeeded; its absence means nothing was restored (the bug signature).
    try:
        st = _state(ctx)
        dlog(ctx, "duck", session=session_id, on=bool(on), mute=bool(mute), muted=st.get("muted"),
             ducked=ducked, jellyfin_saved=st.get("jellyfin_video_volume"),
             tidal_prior=st.get("tidal_prior"), dur_ms=int((time.monotonic() - _t0) * 1000),
             win=st.get("duck_window_seq"), win_open=st.get("duck_window_open"), win_ms=win_ms)
    except Exception:
        pass
    return {"ok": True, "ducked": ducked}


def note_duck_activity(ctx) -> None:
    """Refresh the duck-watchdog heartbeat — call on any incoming voice activity (an utterance reaching the
    brain, addressed or aside). Keeps the watchdog from firing during a legitimate sustained hold: while Rob
    is speaking/dictating his turns keep this fresh, so the auto-restore only triggers in genuine SILENCE
    after a duck that was never released. Best-effort; never raises."""
    try:
        _state(ctx)["last_duck_refresh_mono"] = time.monotonic()
    except Exception:
        pass


def _duck_watchdog_secs(ctx) -> int:
    """Seconds a duck may stay open with no refresh before the watchdog auto-restores it (config
    duck_watchdog_secs, default 12). 0 disables. Clamped so it can't be set absurdly tight and fight a normal
    bot announcement."""
    try:
        v = int(getattr(ctx.config, "duck_watchdog_secs", 12))
    except Exception:
        v = 12
    if v <= 0:
        return 0
    return max(5, v)


async def _duck_watchdog_check(ctx) -> None:
    """Safety net for a voice-side on-without-off: if a duck window has been open longer than the configured
    grace with NO refresh (no duck-on, no incoming voice turn), force a restore so the movie can't sit stranded
    at the duck floor in silence. Ticked by the ~1 Hz /media/state poll — no background task. Best-effort.

    This is belt-and-suspenders; the correct fix is the voice side pairing every duck-on with an off. The
    watchdog only fires on genuine silence — note_duck_activity refreshes it on every utterance, so a real
    sustained hold during dictation is never cut."""
    secs = _duck_watchdog_secs(ctx)
    if not secs:
        return
    try:
        st = _state(ctx)
        if not st.get("duck_window_open"):
            return
        last = st.get("last_duck_refresh_mono")
        if last is None:
            return
        held = time.monotonic() - last
        if held < secs:
            return
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "duck_watchdog", phase="auto_restore", held_secs=round(held, 1), grace=secs)
        await duck_media(ctx, on=False)
    except Exception:
        pass


async def media_state(ctx) -> dict:
    """Provider-NEUTRAL playback snapshot for the voice client's duck-timing. The shape is generic by
    design — NO brain-specific provider names (jellyfin/tidal/…) cross the brain↔voice protocol, so any
    brain can serve it and the voice side never learns what media the brain controls (see
    [[feedback-gabagent-brain-agnostic-protocol]]). Returns {"playing": bool, "state":
    "playing"|"paused"|"idle", "kind": "audio"|"video"|None}. `kind` is a generic media TYPE (not a
    provider name) so the gate can avoid locking the user out of a video they're actively watching.
    Never raises."""
    from gabagent.commands.media import inventory, local_audible
    _t0 = time.monotonic()
    srcs = await inventory(ctx)
    _t1 = time.monotonic()
    # SCOPE: only LOCAL audible media drives the snapshot. A media source on another device/room is
    # excluded by design — it isn't in this box's audio loop, so it must not drive `kind` or gate the mic.
    # (This is the structural fix for the cross-device kind-flap that thrashed the voice gate.)
    local = local_audible(srcs)
    playing = any(s.state == "playing" for s in local)
    state = "playing" if playing else ("paused" if any(s.state == "paused" for s in local) else "idle")
    # Generic media KIND — "audio"|"video"|None. Brain-agnostic (a media TYPE, not a provider name), so it
    # doesn't leak which provider is in play; lets the gate skip locking the user out of a video they're
    # watching. Prefer an actively-playing source; a paused one is the fallback; video outranks audio.
    kind = _pick_kind(local)
    # Brain-internal debug only (NOT part of the neutral protocol response): local-vs-remote counts explain
    # a "nothing local playing" snapshot and surface a remote source that's correctly being ignored.
    try:
        from gabagent.voice.debuglog import dlog
        # Per-source breakdown ("provider:state:L|R") so an idle⇄playing flap is attributable to the exact
        # provider that dropped/reappeared — the 06-05 schema only logged opaque counts, which couldn't
        # root-cause VAC's gate flap. With this, a transient (e.g. a Mopidy album-gap "stopped" read, or an
        # MPRIS source blinking out) names itself in the log.
        srcs_brief = [f"{s.provider}:{s.state}:{'L' if s.is_local else 'R'}" for s in srcs]
        dlog(ctx, "media_state", playing=playing, state=state, kind=kind,
             local=len(local), total=len(srcs),
             remote=sum(1 for s in srcs if s.locality == "remote"),
             srcs=srcs_brief, ms=int((_t1 - _t0) * 1000))
    except Exception:
        pass
    # Heartbeat for the duck watchdog — restore a duck left open with no refresh (a voice-side on-without-off).
    await _duck_watchdog_check(ctx)
    return {"playing": playing, "state": state, "kind": kind}


def _pick_kind(sources) -> str | None:
    """Generic media TYPE for the gate from a set of (local, audible) sources: prefer an actively-playing
    source, video outranks audio, a paused source is the fallback. Stays brain-neutral."""
    for want_state in ("playing", "paused"):
        group = [s for s in sources if s.state == want_state]
        if any(s.kind == "video" for s in group):
            return "video"
        if any(s.kind == "audio" for s in group):
            return "audio"
    return None


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


def _jf_dlog(ctx, **fields) -> None:
    """Best-effort instrumentation for the Jellyfin movie sink duck/volume (so the next live run proves the
    browser sink-input was FOUND and the level took, vs. silently no-op'ing). Gated by the voice_debug flag."""
    try:
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "jellyfin_duck", **fields)
    except Exception:
        pass


async def _run_cmd(*args, timeout: float = 2.0) -> tuple[int, str]:
    """Generic best-effort subprocess (used for pgrep). Mirrors _run_pactl's never-raise contract."""
    try:
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
        return p.returncode or 0, out.decode(errors="replace")
    except Exception:
        return 1, ""


def _proc_descendants(pids: set[str]) -> set[str]:
    """Expand PIDs to include all descendants. Chromium plays audio from a child *audio service* process,
    so the sink-input is owned by a descendant of the main browser PID, not the main PID itself. Reads
    /proc/<pid>/task/<pid>/children; best-effort (a vanished pid just contributes nothing)."""
    seen = set(pids)
    frontier = list(pids)
    while frontier:
        pid = frontier.pop()
        try:
            with open(f"/proc/{pid}/task/{pid}/children") as f:
                kids = f.read().split()
        except Exception:
            kids = []
        for k in kids:
            if k not in seen:
                seen.add(k)
                frontier.append(k)
    return seen


def _pid_matched_sinks(out: str, pids: set[str]) -> list[dict]:
    """Every sink-input whose application.process.id is one of `pids` — OUR browser's audio streams by
    process identity (not a name another Chrome could share). Usually one; a stale/corked sibling tab from
    a prior play attempt can leave a second."""
    res = []
    for si in _parse_sink_inputs(out):
        m = re.search(r'application\.process\.id = "?(\d+)"?', si["block"])
        if m and m.group(1) in pids:
            res.append(si)
    return res


def _sink_is_active(si: dict) -> bool:
    """Is this sink-input the actively-playing stream (vs a paused/idle leftover)? `corked` is authoritative
    where present (PipeWire: Corked:no = playing); `state==RUNNING` is the PulseAudio fallback. Unknown on
    both → not treated as active, so it never out-ranks a known-active sibling."""
    if si.get("corked") is not None:
        return si["corked"] is False
    return si.get("state") == "RUNNING"


def _choose_sink(cands: list[dict]) -> tuple[str, int | None] | None:
    """Pick the AUDIBLE stream among our browser's sink-inputs. When the tree owns more than one — a
    stale sibling (a prior attempt / a still-open older window) alongside the live movie — prefer the
    one that's actually PLAYING (uncorked) over pactl's list order, which can put the silent leftover first.
    That ordering bug ducked the wrong sink for a whole movie window (the live stream stayed at full volume).

    When two siblings are EQUALLY active — both uncorked, so `corked` can't tell them apart (the leftover
    from a prior attempt sometimes still reads `Corked: no`) — break the tie by sink-input INDEX: the highest
    is the most recently created stream, i.e. the just-started movie; the leftover is older and lower. This is
    the "doesn't duck the first time, works the second" failure — pactl listed the older sibling first, we
    ducked it, the movie stayed at 100%, and only once the leftover stream went away did the next wake duck the
    real one. Falls back to the first match only when none looks active (movie paused, every candidate corked —
    restore/seek still need a target)."""
    if not cands:
        return None
    active = [c for c in cands if _sink_is_active(c)]
    if active:
        chosen = max(active, key=lambda c: int(c["idx"]))
    else:
        chosen = cands[0]
    return chosen["idx"], chosen["volume"]


def _parse_sink_input_by_pid(out: str, pids: set[str]) -> tuple[str, int | None] | None:
    """The audible sink-input owned by our browser process tree → (idx, vol%), or None. RUNNING-preferred
    when the tree owns several (see _choose_sink)."""
    return _choose_sink(_pid_matched_sinks(out, pids))


def _parse_lone_browser_sink(out: str) -> tuple[str, int | None] | None:
    """Fallback when the PID match finds nothing: a SINGLE Chromium/Chrome sink-input. Only trusted when
    there's exactly one — more than one is ambiguous (the user's separate Chrome), so we decline rather
    than risk attenuating the wrong stream."""
    hits = [si for si in _parse_sink_inputs(out)
            if re.search(r'application\.name = "(Chromium|Google Chrome|Chrome)"', si["block"])]
    return (hits[0]["idx"], hits[0]["volume"]) if len(hits) == 1 else None


async def _jellyfin_sink_input(ctx) -> tuple[str, int | None] | None:
    """Our browser-played movie's PipeWire sink-input (idx, vol%), or None.

    Identified by the process tree of OUR persistent browser context: its --user-data-dir
    (config_dir/browser/jellyfin) is unique to us, so matching the sink-input's owning process against that
    tree separates our Chromium from any SEPARATE Chrome the user runs (name-matching alone can't). Falls
    back to a lone Chromium/Chrome sink-input only when the PID match finds nothing. Best-effort; never
    raises. Re-discovered each call (the index changes when the movie restarts), mirroring _mopidy_sink_input."""
    if not shutil.which("pactl"):
        return None
    rc2, out = await _run_pactl("list", "sink-inputs")
    if rc2 != 0 or not out:
        return None
    from gabagent.config.paths import config_dir
    profile_dir = str(config_dir() / "browser" / "jellyfin")
    rc, pout = await _run_cmd("pgrep", "-f", profile_dir)
    main_pids = {p for p in pout.split() if p.isdigit()} if rc == 0 else set()
    if main_pids:
        cands = _pid_matched_sinks(out, _proc_descendants(main_pids))
        # Stash the candidate set (idx/app/state/vol) so the sink_on dlog can show WHY a sink was chosen —
        # a stale-sibling mispick is then visible in one line instead of needing a re-mine.
        _state(ctx)["jellyfin_sink_cands"] = [
            {"idx": c["idx"], "app": c["app"], "state": c["state"], "corked": c["corked"],
             "muted": c.get("muted"), "vol": c["volume"]} for c in cands]
        hit = _choose_sink(cands)
        if hit:
            return hit
    _state(ctx).pop("jellyfin_sink_cands", None)
    # PID match found nothing. If we OWN a movie page, that match is AUTHORITATIVE — a transient miss (the
    # owned <video> paused/re-buffered, so PipeWire briefly dropped its sink-input, leaving only the user's
    # SEPARATE Chrome as the "lone" browser) must NOT fall through to _parse_lone_browser_sink: that grabs the
    # wrong stream and ducks/MUTES a different movie (the sink-420 bug — silenced another movie while the owned
    # one stayed at 100%). Duck nothing this cycle; the next poll recovers when our sink reappears. The lone
    # fallback is kept ONLY for the no-owned-page recovery case (e.g. a page ref lost across a brain restart).
    from gabagent.commands.providers.jellyfin import _live_jellyfin_page
    if _live_jellyfin_page(ctx) is not None:
        return None
    return _parse_lone_browser_sink(out)


async def ensure_jellyfin_sink_audible(ctx, *, lift_volume: bool = False) -> None:
    """Make our browser movie's PipeWire sink-input(s) actually AUDIBLE — two layers the <video> unmute
    (_ensure_video_audible) and the duck can't reach on their own:

    1. MUTE flag (always): clear a stranded sink-input `Mute: yes`. The duck attenuates by VOLUME only and
       never sets the mute flag, but a reused browser stream can carry one — silent regardless of
       <video>.muted/volume OR the sink level. Mirrors _unmute_sink_input for Mopidy.
    2. STRANDED-LOW volume (`lift_volume=True`, fresh play only): PipeWire's stream-restore replays the PRIOR
       movie's ducked/low level (e.g. 18%, the duck floor) onto the brand-new stream, so the movie plays quiet
       even though <video>.volume was reset to 1.0 — the sink layer is separate. Raise such a sink UP to the
       configured start baseline (_movie_start_volume). Only on a fresh play (resume keeps a level the user
       set), only ACTIVE/uncorked streams, only UP (never lower a movie, never touch a corked leftover). If a
       duck already owns the sink, fix its SAVED restore-baseline instead of raising the live ducked level —
       so the movie returns to baseline on release without a loud blip mid-speech.

    Scoped to OUR browser PID tree so it never touches another Chrome or the TTS stream. Always logs a
    `jellyfin_audible` line when our sinks are found (even with nothing to do) so a run can PROVE which layer
    was the culprit. Best-effort; never raises."""
    if not shutil.which("pactl"):
        return
    try:
        rc, out = await _run_pactl("list", "sink-inputs")
        if rc != 0 or not out:
            return
        from gabagent.config.paths import config_dir
        profile_dir = str(config_dir() / "browser" / "jellyfin")
        rc2, pout = await _run_cmd("pgrep", "-f", profile_dir)
        main_pids = {p for p in pout.split() if p.isdigit()} if rc2 == 0 else set()
        if not main_pids:
            return
        cands = _pid_matched_sinks(out, _proc_descendants(main_pids))
        if not cands:
            return
        st = _state(ctx)
        baseline = _movie_start_volume(ctx)
        duck_prior = st.get("jellyfin_sink_prior")
        cleared, raised = [], []
        for c in cands:
            if c.get("muted") is True:
                await _run_pactl("set-sink-input-mute", c["idx"], "0")
                cleared.append(c["idx"])
            if lift_volume and _sink_is_active(c):
                vol = c.get("volume")
                if vol is not None and vol < baseline:
                    if duck_prior is not None and str(duck_prior[0]) == c["idx"]:
                        # A duck owns this sink — don't raise the live (ducked) level; fix the restore baseline.
                        st["jellyfin_sink_prior"] = (duck_prior[0], baseline)
                    else:
                        await _run_pactl("set-sink-input-volume", c["idx"], f"{baseline}%")
                    raised.append(c["idx"])
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "jellyfin_audible", cleared_mute=cleared, raised_to_baseline=raised,
             baseline=baseline, lift=lift_volume,
             cands=[{"idx": c["idx"], "muted": c.get("muted"), "corked": c.get("corked"),
                     "vol": c["volume"]} for c in cands])
    except Exception:
        pass


def _parse_mopidy_sink_full(out: str) -> dict | None:
    """Richer Mopidy sink-input parse for the audibility probe: index, volume %, the MUTE flag, and the
    sink index it's routed to. A loaded track reads "playing" from the player even when this stream is
    muted / at 0% / on a non-default sink, so the verify path needs these fields to tell whether the user
    can actually HEAR it. Returns None when no Mopidy stream is at the sink."""
    for block in out.split("Sink Input #")[1:]:
        if 'application.name = "Mopidy"' not in block and 'node.name = "Mopidy"' not in block:
            continue
        idx = block.splitlines()[0].strip()
        if not idx.isdigit():
            continue
        vm = re.search(r"Volume:.*?(\d+)%", block)
        mm = re.search(r"Mute:\s*(yes|no)", block)
        sm = re.search(r"^\s*Sink:\s*(\d+)", block, re.MULTILINE)
        return {
            "idx": idx,
            "volume": int(vm.group(1)) if vm else None,
            "muted": (mm.group(1) == "yes") if mm else None,
            "sink_idx": sm.group(1) if sm else None,
        }
    return None


async def _default_sink_name() -> str | None:
    rc, out = await _run_pactl("get-default-sink")
    name = out.strip()
    return name if rc == 0 and name else None


async def _sink_name_by_index(sink_idx: str | None) -> str | None:
    if not sink_idx:
        return None
    rc, out = await _run_pactl("list", "short", "sinks")
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == sink_idx:
            return parts[1]
    return None


async def _sink_mute_volume(sink: str | None) -> tuple[bool | None, int | None]:
    """The DEFAULT sink's OWN mute flag and volume %, read from `pactl get-sink-mute/-volume`. A music
    sink-input can be present, unmuted and at 100% yet DEAD SILENT if the sink it feeds is muted or at 0% —
    the Mopidy player still reports "playing" (live 2026-06-18: "It died." → brain said "sounds audible").
    Returns (muted, volume%); EITHER is None when unreadable so the caller stays neutral and never
    false-claims silence. PipeWire's pactl emits `Mute: yes|no` and `Volume: … /  NN% / …`. Never raises."""
    if not sink:
        return (None, None)
    muted: bool | None = None
    vol: int | None = None
    try:
        rc, out = await _run_pactl("get-sink-mute", sink)
        if rc == 0 and out:
            m = re.search(r"Mute:\s*(yes|no)", out)
            if m:
                muted = (m.group(1) == "yes")
        rc, out = await _run_pactl("get-sink-volume", sink)
        if rc == 0 and out:
            m = re.search(r"(\d+)%", out)
            if m:
                vol = int(m.group(1))
    except Exception:
        pass
    return (muted, vol)


async def _unmute_sink_input(idx: str) -> None:
    """Clear a stray MUTE flag on a music sink-input. Our duck scheme attenuates by VOLUME only and never
    sets the mute flag, so on the audible-restore/play paths the music stream should never carry one; if
    something else (stream-restore replay, a stray toggle) set it, a volume restore alone leaves the stream
    silent at a healthy level — the 'set it to 50% and still nothing' signature. Best-effort; never raises."""
    await _run_pactl("set-sink-input-mute", idx, "0")


async def ensure_mopidy_sink_audible(ctx, *, attempts: int = 6, delay: float = 0.5) -> None:
    """Clear a stranded MUTE flag on the Mopidy (Tidal) sink-input so resumed/started music isn't silent.
    The Tidal analog of ensure_jellyfin_sink_audible: the duck attenuates by VOLUME and apply_ambient_cap
    mirrors the mixer onto the sink-input LEVEL, but neither touches the per-sink-input `Mute:` flag.
    PipeWire stream-restore (or a stray toggle) can bring a stream up muted, so the music plays silent at a
    healthy mixer/level until a manual volume command happens to unmute it (Rob, live 2026-06-15: "no sound
    until I turned up the volume" — mixer was 70 the whole time).

    TIMING: called from _hold_ambient at play/resume time, but the Mopidy sink-input does NOT exist yet —
    it materializes a few seconds later when audio starts flowing (live 2026-06-16: a one-shot check at
    play time always found nothing and no-op'd, so the fix was inert). So POLL briefly for the sink-input
    to appear, then unmute. Called fire-and-forget by _hold_ambient so this poll never blocks playback.
    Issues an idempotent unmute and logs `mopidy_audible` (the read `was_muted`, plus the `attempt` it was
    found on) so a stranded mute is provable. Best-effort; never raises."""
    if not shutil.which("pactl"):
        return
    from gabagent.voice.debuglog import dlog
    n = max(1, attempts)
    for attempt in range(n):
        try:
            rc, out = await _run_pactl("list", "sink-inputs")
            si = _parse_mopidy_sink_full(out) if (rc == 0 and out) else None
            if si is not None:
                was_muted = si.get("muted")
                await _unmute_sink_input(si["idx"])     # idempotent; guarantees the stream isn't left muted
                dlog(ctx, "mopidy_audible", idx=si["idx"], was_muted=was_muted,
                     vol=si.get("volume"), sink=si.get("sink_idx"), attempt=attempt)
                return
        except Exception:
            pass
        if attempt + 1 < n:
            await asyncio.sleep(delay)
    # Sink-input never appeared within the window (playback didn't start, or stopped) — nothing to unmute.


async def mopidy_audibility() -> dict:
    """Probe whether the Mopidy music stream is actually AUDIBLE — present, unmuted, >0 volume, AND routed
    to the current default sink — rather than just "a track is loaded" (which the player reports even into
    total silence). Used by tidal.now_playing so the brain grounds "is it playing?" in real output state and
    stops asserting playback the user can't hear (the "I verified it's playing, check your sound settings"
    over-claim). Conservative: only reports inaudible on POSITIVE evidence (muted / 0% / provably off the
    default sink); when it can't tell, it does not claim silence. Best-effort — on any probe failure returns
    {"checked": False} so callers stay neutral. Never raises."""
    try:
        if not shutil.which("pactl"):
            return {"checked": False}
        rc, out = await _run_pactl("list", "sink-inputs")
        if rc != 0 or not out:
            return {"checked": False}
        si = _parse_mopidy_sink_full(out)
        if si is None:
            return {"checked": True, "present": False, "audible": False,
                    "reason": "there's no music stream at the audio output"}
        default_sink = await _default_sink_name()
        sink_name = await _sink_name_by_index(si["sink_idx"])
        vol, muted = si["volume"], si["muted"]
        off_default = bool(sink_name and default_sink and sink_name != default_sink)
        # The MASTER sink itself: a healthy sink-input feeding a muted / 0% default sink is still silent,
        # and the player keeps reporting "playing". Without this the verify path over-claimed audible into
        # dead silence (#3) and the brain couldn't tell the user to unmute the SINK (it kept nudging
        # stream levels — #2). Only counted as POSITIVE evidence of silence when actually read (True / 0);
        # unreadable (None) stays neutral, per this probe's conservative contract.
        sink_muted, sink_vol = await _sink_mute_volume(default_sink)
        reasons = []
        if muted:
            reasons.append("the music output is muted")
        if vol == 0:
            reasons.append("its volume is at zero")
        if off_default:
            reasons.append(f"it's routed to '{sink_name}', not your active output")
        if sink_muted is True:
            reasons.append("your speakers are muted")
        if sink_vol == 0:
            reasons.append("your speaker volume is at zero")
        audible = not (muted or vol == 0 or off_default or sink_muted is True or sink_vol == 0)
        res = {"checked": True, "present": True, "audible": audible,
               "volume": vol, "muted": muted, "sink": sink_name, "default_sink": default_sink,
               "on_default": (not off_default), "sink_muted": sink_muted, "sink_volume": sink_vol}
        if reasons:
            res["reason"] = "; ".join(reasons)
        return res
    except Exception:
        return {"checked": False}


def _parse_sink_inputs(out: str) -> list[dict]:
    """Every sink-input in `pactl list sink-inputs` as {idx, volume %, app, state, corked, block text} — the
    block is kept so callers can test it for exclusion markers (TTS property / Mopidy ownership).

    Activity discriminator: `corked` is the reliable one. PipeWire's pactl (17.x) emits `Corked: no` for the
    actively-playing stream and `Corked: yes` for a paused/idle leftover, but — unlike PulseAudio — emits NO
    per-sink-input `State:` line at all (only *sinks* carry State), so `state` reads None on PipeWire. The duck
    must therefore prefer the UNCORKED stream when our browser owns several; `state==RUNNING` is kept only as a
    PulseAudio-portability fallback (see _choose_sink).

    `muted` is the per-sink-input `Mute:` flag — orthogonal to volume and to `<video>.muted`. A muted
    sink-input is silent no matter the sink level OR the element volume, so a fresh movie that inherits a
    stranded mute (e.g. the reused browser context after closing a prior movie) plays silently and a 'turn it
    up' can't recover it — the muted-on-fresh-play bug. Surfaced here so the play/resume paths can clear it."""
    items = []
    for block in out.split("Sink Input #")[1:]:
        idx = block.splitlines()[0].strip()
        if not idx.isdigit():
            continue
        m = re.search(r"Volume:.*?(\d+)%", block)
        am = re.search(r'application\.name = "([^"]+)"', block)
        stm = re.search(r"^\s*State:\s*(\w+)", block, re.MULTILINE)
        cm = re.search(r"^\s*Corked:\s*(yes|no)", block, re.MULTILINE)
        mm = re.search(r"^\s*Mute:\s*(yes|no)", block, re.MULTILINE)
        items.append({"idx": idx, "volume": int(m.group(1)) if m else None,
                      "app": am.group(1) if am else None,
                      "state": stm.group(1) if stm else None,
                      "corked": (cm.group(1) == "yes") if cm else None,
                      "muted": (mm.group(1) == "yes") if mm else None, "block": block})
    return items


def _excluded_from_local_duck(block: str) -> bool:
    """A sink-input the universal local-duck must NOT touch: the assistant's own TTS output (never mute
    Aria) or the owned Mopidy stream (ducked separately, so it isn't double-managed)."""
    b = block.lower()
    if _TTS_EXCLUDE_PROP.lower() in b:                     # the property VAC stamps on its TTS stream
        return True
    if "alsa_playback" in b or "voice-agent" in b:        # safety net for the TTS stream pre-stamp
        return True
    if 'application.name = "mopidy"' in b or 'node.name = "mopidy"' in b:   # owned, handled by _duck_tidal_sink
        return True
    return False


async def _duck_local_sinks(ctx, on: bool, mute: bool = False) -> bool:
    """Universal local-media duck: on a full-mute window-open, drop EVERY other local media sink-input to 0
    (saving each prior), and restore them on close — so Rob is heard over ANY local media. Only fires on
    `mute` (the open-window suppression); the gentle VAD-onset duck leaves unowned media alone. Excludes the
    TTS stream and the owned Mopidy stream. Best-effort; never raises. Returns True if it acted."""
    st = _state(ctx)
    try:
        if on:
            if not mute or st.get("local_sink_priors"):    # only on a fresh full-mute; idempotent
                return False
            if not shutil.which("pactl"):
                return False
            rc, out = await _run_pactl("list", "sink-inputs")
            if rc != 0 or not out:
                return False
            # The owned movie sink is ducked separately by _duck_jellyfin_sink (which ran just before this
            # in duck_media and saved its idx) — exclude it here so the two don't both save/restore it.
            jf_saved = st.get("jellyfin_sink_prior")
            owned_jf_idx = jf_saved[0] if jf_saved else None
            priors = {}
            for si in _parse_sink_inputs(out):
                if si["volume"] is None or si["idx"] == owned_jf_idx \
                        or _excluded_from_local_duck(si["block"]):
                    continue
                await _run_pactl("set-sink-input-volume", si["idx"], "0%")
                priors[si["idx"]] = si["volume"]
            if not priors:
                return False
            st["local_sink_priors"] = priors
            return True
        priors = st.get("local_sink_priors")
        if not priors:
            return False
        for idx, prior in priors.items():
            await _run_pactl("set-sink-input-volume", idx, f"{prior}%")
        st["local_sink_priors"] = None
        return True
    except Exception:
        return False


def _ambient_cap(ctx) -> int:
    """The % ceiling to hold playing music at continuously (config media_ambient_cap, default 90).
    100 disables the ambient cap. Clamped to a sane range."""
    try:
        c = int(getattr(ctx.config, "media_ambient_cap", 90))
    except Exception:
        c = 90
    return max(1, min(100, c))


def _movie_start_volume(ctx) -> int:
    """The sink-input % a freshly-played movie should start at (config movie_start_volume, default 100 =
    neutral, no per-stream attenuation; the device/player volume governs absolute loudness). Lower it to make
    movies start gentler. Clamped to [20, 100] so a bad config can't strand a fresh movie inaudible (20 sits
    just above the duck floor)."""
    try:
        v = int(getattr(ctx.config, "movie_start_volume", 100))
    except Exception:
        v = 100
    return max(20, min(100, v))


async def apply_ambient_cap(ctx, new_track: bool = True) -> None:
    """Cap currently-playing music at the ambient ceiling so VAD can hear the user over it, AND mirror
    the (capped) software-mixer level onto the PipeWire sink-input. Called when music starts.

    The mixer itself is only LOWERED to the cap (never raised above the user's chosen level). The
    sink-input, by contrast, is MIRRORED to the mixer — raised or lowered to match. That mirror is the
    fix for the stranded-quiet track: a speech-duck sets the Mopidy sink-input to ~18%, PipeWire's
    module-stream-restore remembers it, and the NEXT track's sink-input comes up at 18%; a lower-only cap
    can't raise it, so the music plays stuck-quiet. Matching the sink-input to the mixer un-strands it.
    Because we mirror the mixer (not force the cap), a genuinely user-lowered volume stays low — no blast.
    Best-effort on both the Mopidy software mixer and the PipeWire sink-input. Never raises."""
    cap = _ambient_cap(ctx)
    if cap >= 100:
        return
    from gabagent.commands.providers.tidal import resolve_tidal
    tc = resolve_tidal(ctx)   # #62: this room's Mopidy endpoint (global when no per-room override)
    try:
        mixer = None
        if tc and getattr(tc, "enabled", False):
            from gabagent.commands.providers.tidal import _rpc
            vol = await _rpc(tc, "core.mixer.get_volume", timeout=2.0)
            if vol is not None:
                mixer = int(vol)
                if mixer > cap:                                # lower-only: never raise the user's level
                    await _rpc(tc, "core.mixer.set_volume", {"volume": cap}, timeout=2.0)
                    mixer = cap
        # A speech duck is in progress — the sink is intentionally at the duck level, so don't mirror the
        # mixer onto it (that would un-duck the bed mid-window). BUT a new track can start mid-duck (Rob
        # rapid-changing songs while the window is open): it gets a FRESH sink-input that stream-restore
        # brings up at a stale level, and the saved prior still points at the old track's index. Left alone
        # it strands — quiet ("I don't hear music") or, worse, blasting over the user. So reconcile a NEW
        # sink-input to the active duck level and re-point the saved tuple at it (keeping the prior level so
        # duck-off still restores the user's real volume). This is the F1 residual fix. (ducking.py F1.)
        saved = _state(ctx).get("tidal_sink_prior")
        if saved is not None:
            old_idx, prior = saved
            # The new track's sink-input appears a BEAT after tidal.play returns (the same race the
            # no-duck path polls through above), so a single lookup here finds the OLD sink or none and
            # the reconcile is missed — the new track then strands at a stale level if the window stays
            # open. POLL for a sink whose index DIFFERS from the ducked one, early-exiting the instant it
            # shows, then duck it to the active level and re-point the saved tuple (keeping the prior
            # LEVEL so duck-off still restores the real volume). On a resume (new_track=False) the same
            # stream is already ducked — single check, no poll, so resume-in-window pays no latency.
            new = None
            tries = _AMBIENT_SETTLE_TRIES if new_track else 1
            for _ in range(tries):
                cur = await _mopidy_sink_input()
                if cur and cur[0] != old_idx:
                    new = cur
                    break
                if not new_track:
                    break
                await asyncio.sleep(_AMBIENT_SETTLE_DELAY)   # new sink not up yet — wait for it
            if new is not None:
                # Duck the new sink to the active level ONLY if a speech-duck window is genuinely OPEN. If it
                # has closed (the saved prior is a stale leftover of the reconcile-vs-off race), ducking here
                # would strand the new track quiet with no off to follow — the "I don't hear anything" bug.
                # In that case restore it to the real (mixer/prior) level and clear the stale prior instead.
                if _state(ctx).get("duck_window_open"):
                    duck_target = 0 if _state(ctx).get("muted") else _SINK_DUCK_PCT
                    await _run_pactl("set-sink-input-volume", new[0], f"{duck_target}%")
                    _state(ctx)["tidal_sink_prior"] = (new[0], prior)
                    _tidal_dlog(ctx, phase="ambient_cap", cap=cap, mixer=mixer,
                                reconciled_sink=new[0], duck_target=duck_target,
                                window_open=True, chose="duck")
                else:
                    restore = min(int(mixer) if mixer is not None else int(prior), cap)
                    await _run_pactl("set-sink-input-volume", new[0], f"{restore}%")
                    await _unmute_sink_input(new[0])
                    _state(ctx)["tidal_sink_prior"] = None
                    _tidal_dlog(ctx, phase="ambient_cap", cap=cap, mixer=mixer,
                                reconciled_sink=new[0], restored_to=restore,
                                window_open=False, chose="restore_mixer")
            else:
                _tidal_dlog(ctx, phase="ambient_cap", cap=cap, mixer=mixer, skipped="ducking",
                            window_open=bool(_state(ctx).get("duck_window_open")))
            return
        # Mirror the (capped) mixer onto the sink-input — fall back to the cap if the mixer is unknown.
        # This RAISES a stranded-low sink-input (stream-restore replayed a duck) back to the real level, and
        # lowers one above the cap. The new track's sink-input can appear a BEAT after play returns and come
        # up stranded-quiet (F1, ~1/3 of starts), so poll for it briefly before mirroring; early-exit once
        # present. (When already present — the common case + every existing test — this is a single pass.)
        target = min(mixer, cap) if mixer is not None else cap
        found = None
        for _ in range(_AMBIENT_SETTLE_TRIES):
            found = await _mopidy_sink_input()
            if found:
                break
            await asyncio.sleep(_AMBIENT_SETTLE_DELAY)   # sink-input not up yet (new track settling) — wait
        if found:
            idx, svol = found
            if svol != target:
                await _run_pactl("set-sink-input-volume", idx, f"{target}%")
            if target > 0:                    # bringing a (new) track to an audible level — clear any stray mute
                await _unmute_sink_input(idx)
        _tidal_dlog(ctx, phase="ambient_cap", cap=cap, mixer=mixer, settled=bool(found))
    except Exception:
        pass


def note_user_volume(ctx, target: int) -> bool:
    """Record a user's absolute volume set against an in-progress duck so restore honors it.

    `tidal.set_volume` sets the live mixer+sink, but if a duck is active (the command window is open, music
    ducked to ~18%) the duck-off restore would otherwise overwrite the user's new level with the stale saved
    prior — the "I turn it down and it blasts back up" bug. When a duck is active, update the saved restore
    priors to `target` and signal the caller to SKIP the live write (option b: the bed stays suppressed through
    the open window, and the new level lands on restore — so a louder mix can't leak into VAD mid-window).

    Returns True if a duck was active (priors updated; caller should skip its live write), False otherwise
    (no duck → caller does its normal live set). Keeps all duck-state mutation in this module. Never raises."""
    try:
        st = _state(ctx)
        if st.get("tidal_prior") is None and st.get("tidal_sink_prior") is None:
            return False
        st["tidal_prior"] = int(target)
        saved = st.get("tidal_sink_prior")
        if saved is not None:
            st["tidal_sink_prior"] = (saved[0], int(target))
        return True
    except Exception:
        return False


async def _duck_tidal_sink(ctx, on: bool, mute: bool = False, mixer_vol: int | None = None) -> None:
    """Belt-and-suspenders for the music duck: also attenuate the Mopidy stream at the SYSTEM node
    (PipeWire sink-input). Mopidy's software mixer proved intermittently inaudible (value changes but
    the output stays loud); the sink-input is the node the user reaches by hand, so it's reliably
    heard. Best-effort, paired with the software-mixer duck in _duck_tidal. Never raises.

    `mute=True` targets 0% instead of the ambient duck; it can also deepen an already-active duck to 0
    without touching the saved prior tuple (so restore still returns to the real level).

    `mixer_vol` is the Mopidy software-mixer level (the source of truth for the user's chosen volume),
    threaded in by _duck_tidal so the sink restore prior mirrors the mixer rather than guessing from the
    sink's own read. None on a standalone call → fall back to the sink read."""
    st = _state(ctx)
    try:
        if on:
            sink_target = 0 if mute else _SINK_DUCK_PCT
            saved = st.get("tidal_sink_prior")
            if saved is not None:
                if not (mute and not st.get("muted")):
                    return  # already ducked, and not a fresh deepen-to-mute → leave it
                # Deepen the existing duck to a full mute; keep the saved prior so restore is unaffected.
                cur = await _mopidy_sink_input()
                tgt_idx = cur[0] if cur else saved[0]
                await _run_pactl("set-sink-input-volume", tgt_idx, "0%")
                _tidal_dlog(ctx, phase="sink_deepen_mute", sink_idx=tgt_idx)
                return
            found = await _mopidy_sink_input()
            if not found:
                return
            idx, vol = found
            # The Mopidy software mixer is the source of truth for the user's chosen level; the sink-input
            # mirrors it. Prefer the threaded mixer level for the restore prior; fall back to the sink's own
            # read only when no mixer was supplied (a standalone call). We no longer fabricate the ambient
            # cap when the sink reads low — that overrode a deliberately-low user level and caused the
            # "turn it down → blasts back to 90" bug. A genuinely stranded-low sink is un-stranded separately
            # by apply_ambient_cap's mirror-on-play; restore still clamps to the cap (line below in off-path).
            prior = mixer_vol if mixer_vol is not None else (vol if vol is not None else 100)
            st["tidal_sink_prior"] = (idx, prior)
            await _run_pactl("set-sink-input-volume", idx, f"{sink_target}%")
            _tidal_dlog(ctx, phase="sink_on", sink_idx=idx, sink_prior=vol, ducked_to=sink_target,
                        saved_prior=prior)
            return
        saved = st.get("tidal_sink_prior")
        if not saved:
            return
        idx, prior = saved
        target = min(int(prior), _ambient_cap(ctx))   # restore to the cap (a ceiling), never above it
        cur = await _mopidy_sink_input()      # re-find: the stream/index may have changed
        tgt_idx = cur[0] if cur else idx
        await _run_pactl("set-sink-input-volume", tgt_idx, f"{target}%")
        await _unmute_sink_input(tgt_idx)     # restore to an AUDIBLE level — clear any stray mute flag
        _tidal_dlog(ctx, phase="sink_off", sink_idx=tgt_idx, restored_to=target)
        st["tidal_sink_prior"] = None
    except Exception:
        pass


def _room_duck_local(ctx) -> bool:
    """True when THIS room ducks its music locally at the sink (a satellite-side PipeWire belt), so the
    brain must NOT also duck via the Mopidy mixer-RPC. #62: set room_media[<room>].duck_local for a
    satellite whose Mopidy software-mixer can't be reliably ducked over RPC. Default false ⇒ the brain
    ducks as before (EM / single-room / unconfigured installs byte-identical)."""
    rid = getattr(ctx, "room_id", None)
    if not rid:
        return False
    prof = (getattr(getattr(ctx, "config", None), "room_media", None) or {}).get(rid)
    return bool(getattr(prof, "duck_local", False)) if prof is not None else False


async def _duck_tidal(ctx, on: bool, mute: bool = False) -> bool:
    from gabagent.commands.providers.tidal import _rpc, resolve_tidal
    # #62: a room that ducks locally at the sink owns its duck — the brain's mixer-RPC duck here is a no-op
    # that would mis-report ducked:["tidal"] and risk saving a phantom-0 prior (restore-to-silence). Skip it
    # entirely (both on and off) so the satellite's sink belt is the sole attenuator for this room.
    if _room_duck_local(ctx):
        _tidal_dlog(ctx, phase="skip_duck_local", on=on)
        return False
    tc = resolve_tidal(ctx)   # #62: duck this room's Mopidy over its own rpc_url (global when no override)
    if not tc or not getattr(tc, "enabled", False):
        return False
    st = _state(ctx)
    try:
        if on:
            target = 0 if mute else _DUCK_VOLUME
            if st["tidal_prior"] is not None:
                # Already ducked. A fresh mute DEEPENS it to 0 without re-reading/clobbering the saved
                # prior; any other repeat is a no-op skip.
                if mute and not st["muted"]:
                    await _rpc(tc, "core.mixer.set_volume", {"volume": 0}, timeout=2.0)
                    await _duck_tidal_sink(ctx, True, mute=True)
                    st["muted"] = True
                    _tidal_dlog(ctx, phase="deepen_mute", prior=st["tidal_prior"])
                    return True
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
            await _rpc(tc, "core.mixer.set_volume", {"volume": target}, timeout=2.0)
            # Thread the just-read mixer level so the sink restore prior mirrors the source of truth.
            await _duck_tidal_sink(ctx, True, mute=mute, mixer_vol=int(vol))
            if mute:
                st["muted"] = True
            _tidal_dlog(ctx, phase="on", state=state, read_vol=int(vol), saved_prior=int(vol),
                        ducked_to=target)
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


async def _duck_jellyfin(ctx, on: bool, mute: bool = False) -> bool:
    jc = getattr(ctx.config, "jellyfin", None)
    if not jc or not getattr(jc, "enabled", True) or not jc.api_key:
        return False
    # A movie WE launched plays in a browser page we own → duck its volume (never pause, so it can't
    # fight a manual pause). Anything else (a native client) falls back to REST pause below.
    from gabagent.commands.providers.jellyfin import _live_jellyfin_page
    page = _live_jellyfin_page(ctx)
    if page is not None:
        return await _duck_jellyfin_owned(ctx, page, on, mute)
    # No brain-OWNED movie page → nothing to AUTO-duck here. A bare /Sessions client may be on ANOTHER
    # device/room; auto-pausing it controlled the wrong screen AND flapped the gate (the cross-device bug).
    # Cross-device transport is Phase-2 EXPLICIT-only — via the inventory + `_duck_jellyfin_rest` (kept).
    return False


async def _duck_jellyfin_owned(ctx, page, on: bool, mute: bool = False) -> bool:
    """Duck/restore the movie WE launched. Prefer the browser's PipeWire sink-input (robust — attenuates
    the real audio regardless of whether Jellyfin's web player honors <video>.volume); fall back to the
    <video>.volume path when the sink can't be identified. On `off`, restore via whichever path is saved."""
    st = _state(ctx)
    if on:
        if st.get("jellyfin_sink_prior") is not None:        # already sink-ducked → stay on the sink path
            return await _duck_jellyfin_sink(ctx, page, True, mute)
        if st.get("jellyfin_video_volume") is not None:      # already video-ducked → stay on the video path
            return await _duck_jellyfin_video(ctx, page, True, mute)
        found = await _jellyfin_sink_input(ctx)
        if found is not None:
            return await _duck_jellyfin_sink(ctx, page, True, mute, found=found)
        return await _duck_jellyfin_video(ctx, page, True, mute)   # sink not found → fall back
    # off: restore whichever path is active.
    if st.get("jellyfin_sink_prior") is not None:
        return await _duck_jellyfin_sink(ctx, page, False, mute)
    return await _duck_jellyfin_video(ctx, page, False, mute)


async def _duck_jellyfin_sink(ctx, page, on: bool, mute: bool = False, found=None) -> bool:
    """Duck/restore the movie at the SYSTEM node (browser PipeWire sink-input) — the reliably-audible path.
    Mirrors _duck_tidal_sink: save the prior on first duck, deepen-to-mute without clobbering it, restore to
    the exact prior on off, and un-strand a sink left low by a brain-restart-mid-duck. Leaves <video>.volume
    untouched (the sink is the sole attenuator, so the two can't compound, and Jellyfin's persisted volume
    isn't disturbed)."""
    st = _state(ctx)
    try:
        if on:
            sink_target = 0 if mute else _SINK_DUCK_PCT
            saved = st.get("jellyfin_sink_prior")
            if saved is not None:
                # Already ducked. A fresh mute DEEPENS to 0 without clobbering the saved prior; else no-op.
                if mute and not st.get("muted"):
                    cur = await _jellyfin_sink_input(ctx)
                    tgt = cur[0] if cur else saved[0]
                    await _run_pactl("set-sink-input-volume", tgt, "0%")
                    st["muted"] = True
                    _jf_dlog(ctx, phase="sink_deepen_mute", sink_idx=tgt)
                    return True
                return False
            found = found or await _jellyfin_sink_input(ctx)
            if not found:
                _jf_dlog(ctx, phase="on_no_sink")
                return False
            idx, vol = found
            # Stranded-low guard: a brain restart mid-duck loses the in-memory prior. If the sink is already
            # at/below the duck level, saving that keeps the movie quiet forever — restore-to-100 instead.
            prior = 100 if (vol is not None and vol <= _SINK_DUCK_PCT) else (vol if vol is not None else 100)
            st["jellyfin_sink_prior"] = (idx, prior)
            await _run_pactl("set-sink-input-volume", idx, f"{sink_target}%")
            if mute:
                st["muted"] = True
            _jf_dlog(ctx, phase="sink_on", sink_idx=idx, sink_prior=vol, ducked_to=sink_target,
                     saved_prior=prior, cands=st.get("jellyfin_sink_cands"))
            return True
        saved = st.get("jellyfin_sink_prior")
        if not saved:
            return False
        idx, prior = saved
        cur = await _jellyfin_sink_input(ctx)     # re-find: the index can change if the movie restarted
        tgt = cur[0] if cur else idx
        # Restore to the EXACT prior (no ambient cap — that's a music ceiling; a movie shouldn't creep down).
        await _run_pactl("set-sink-input-volume", tgt, f"{int(prior)}%")
        await _unmute_sink_input(tgt)
        _jf_dlog(ctx, phase="sink_off", sink_idx=tgt, restored_to=int(prior))
        st["jellyfin_sink_prior"] = None
        return True
    except Exception:
        return False


async def adjust_movie_volume(ctx, page, up: bool, step_pct: int = 15) -> bool:
    """Manual movie volume ±step. Drives the browser sink-input if it can be identified (robust), else the
    <video>.volume element. Returns True if it acted, False if it couldn't read a level. Never raises.

    Duck-aware: a volume command is itself spoken, so the speech-onset duck has ALREADY pulled the sink down
    to the duck level (~18%) before this tool runs. Reading that live value and stepping from it is wrong — it
    muted the movie ("down ×2" → 18→3→0) and the speech-end restore then wrote the corrupted value back as the
    new baseline. So while a duck is active we step the SAVED un-ducked baseline (what the movie returns to)
    and leave the ducked sink untouched; the restore brings it to the adjusted baseline. Only when no duck is
    active do we read/write the live level directly."""
    from gabagent.commands.providers.jellyfin import _video_volume, _set_video_volume
    st = _state(ctx)
    try:
        found = await _jellyfin_sink_input(ctx)
        if found is not None:
            idx, live = found
            saved = st.get("jellyfin_sink_prior")
            if saved is not None:
                # Duck active: step the baseline, NOT the ducked sink reading; restore will apply it.
                base = saved[1] if saved[1] is not None else 100
                new = min(100, base + step_pct) if up else max(0, base - step_pct)
                st["jellyfin_sink_prior"] = (saved[0], new)
                _jf_dlog(ctx, phase="manual_sink_ducked", sink_idx=saved[0], baseline=base, set_to=new, up=up)
                return True
            cur = live if live is not None else 100
            new = min(100, cur + step_pct) if up else max(0, cur - step_pct)
            await _run_pactl("set-sink-input-volume", idx, f"{new}%")
            _jf_dlog(ctx, phase="manual_sink", sink_idx=idx, prev=cur, set_to=new, up=up)
            return True
        saved_v = st.get("jellyfin_video_volume")
        if saved_v is not None:
            # Same baseline logic on the <video> fallback path (value is 0.0–1.0).
            new = min(1.0, saved_v + step_pct / 100.0) if up else max(0.0, saved_v - step_pct / 100.0)
            st["jellyfin_video_volume"] = new
            _jf_dlog(ctx, phase="manual_video_ducked", baseline=saved_v, set_to=new, up=up)
            return True
        cur = await _video_volume(page)
        if cur is None:
            return False
        step = step_pct / 100.0
        new = min(1.0, cur + step) if up else max(0.0, cur - step)
        await _set_video_volume(page, new)
        _jf_dlog(ctx, phase="manual_video", prev=cur, set_to=new, up=up)
        return True
    except Exception:
        return False


async def _duck_jellyfin_video(ctx, page, on: bool, mute: bool = False) -> bool:
    from gabagent.commands.providers.jellyfin import _video_volume, _set_video_volume
    st = _state(ctx)
    try:
        if on:
            target = 0.0 if mute else _DUCK_VIDEO_VOLUME
            if st["jellyfin_video_volume"] is not None:
                # Already ducked. A fresh mute DEEPENS to 0.0 without clobbering the saved prior.
                if mute and not st["muted"]:
                    ok = await _set_video_volume(page, 0.0)
                    if ok:
                        st["muted"] = True
                    return ok
                return False  # already ducked — don't clobber the saved level
            prior = await _video_volume(page)
            if prior is None:
                return False
            # If the video is already AT or below the duck level, it was almost certainly stranded
            # there by a brain restart mid-duck (the saved prior is in-memory, lost on restart).
            # Saving that as the restore target would keep the movie quiet forever — restore to full.
            st["jellyfin_video_volume"] = 1.0 if float(prior) <= _DUCK_VIDEO_VOLUME else float(prior)
            await _set_video_volume(page, target)
            if mute:
                st["muted"] = True
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


async def _duck_jellyfin_rest(ctx, jc, on: bool, mute: bool = False) -> bool:
    # A REST-controlled client is PAUSED to duck (already fully silent), so `mute` is a no-op here —
    # the param exists only to keep the provider signatures uniform.
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
