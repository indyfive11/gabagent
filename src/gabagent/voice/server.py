"""HTTP+SSE brain-protocol server for voice mode.

Binds 127.0.0.1 by default (brain + voice front-end share a host). Can bind a specific LAN IP
(`voice_host` / --voice-host) to serve a remote thin satellite; pair that with `voice_auth_token`
so the LAN-exposed endpoints require a shared-secret bearer token (see `_BearerAuth`).

Endpoints:
  GET  /health   -> {"status":"ok","mode":"voice"}
  POST /respond  -> starts a turn; SSE streams VoiceEvents until confirm/done
  POST /confirm  -> resolves a paused confirm; SSE streams the continuation
  POST /confirm  -> resolve a paused confirmation
  POST /cancel   -> abort the in-flight turn (barge-in)

Starlette + uvicorn are an OPTIONAL dependency ([voice] extra); imported lazily so the
base install never breaks. uvicorn runs embedded in the existing asyncio loop via
Server.serve() (never uvicorn.run()), with its signal handlers suppressed.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _coerce_room_id(value):
    """Optional durable room-routing key off a payload. A non-empty string is kept (stripped);
    anything else (None, garbage type, empty) => None. Like the `wake` field, a stray value can
    never perturb a turn — absent/garbage is exactly the single-room default."""
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


class _BearerAuth:
    """Pure-ASGI bearer-token guard for a non-loopback (LAN) brain bind.

    When a token is configured every endpoint EXCEPT /health requires `Authorization: Bearer <token>`
    (constant-time compared); anything else gets 401. /health stays open as an unauthenticated liveness
    probe (it leaks nothing). Pure ASGI — not BaseHTTPMiddleware — so it only inspects request headers
    and never wraps the response body: the SSE streams (`/respond`, `/confirm`) pass through untouched.
    No token (the default, loopback) => this is never installed and the server is fully open, as before.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        import hmac
        headers = dict(scope.get("headers") or ())
        presented = headers.get(b"authorization", b"").decode("latin-1")
        if presented and hmac.compare_digest(presented, self._expected):
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse
        await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)


def build_app(ctx: AgentContext):
    """Build the Starlette app. Exposes the session registry on app.state.sessions
    so it can be driven in tests without a real socket server."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse, StreamingResponse

    from gabagent.voice.session import VoiceSession
    from gabagent.voice.turn import start_turn, drain

    sessions: dict[str, VoiceSession] = {}
    _prewarm_last: dict[str, float] = {}   # room → last pre-warm monotonic time (per-room cooldown)

    def get_session(sid: str) -> VoiceSession:
        vs = sessions.get(sid)
        if vs is None:
            vs = VoiceSession(sid, ctx, ctx.voice_audit_path)
            sessions[sid] = vs
        return vs

    def _busy(vs) -> bool:
        return vs.turn_task is not None and not vs.turn_task.done()

    def _sse(event_gen):
        async def gen():
            async for ev in event_gen:
                yield ev.sse()
        return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def health(request):
        return JSONResponse({"status": "ok", "mode": "voice"})

    async def respond(request):
        body = await request.json()
        sid = body.get("session_id", "default")
        text = body.get("text", "")
        # Item C: optional out-of-band acoustic wake signal (voice side attaches it only on a fresh
        # acoustic wake-open where its own text wake-strip failed). A dict {bare_wake_likelihood,
        # confidence, post_wake_voiced_ms, speech_dur_ms}; absent => exact current behavior. Only a dict
        # is honored (a stray non-object can't perturb the turn).
        wake = body.get("wake")
        if not isinstance(wake, dict):
            wake = None
        vs = get_session(sid)
        # Optional durable room key (multi-room foundation). Refresh-only: a payload that omits it must
        # not clobber a room_id already set by /attach. Ignored for routing today (single-conversation
        # brain) — kept fresh on the session for the future per-room ctx. See call-out 2 in the seam ADR.
        rid = _coerce_room_id(body.get("room_id"))
        if rid is not None:
            vs.room_id = rid
            ctx.room_id = rid  # mirror onto ctx so the tiered-memory layer recalls this room's tier
        ctx.voice_session = vs
        # Marks that the brain RECEIVED an utterance — fires even on a 409 busy (no turn_start follows
        # then). Its ABSENCE after a wake means the voice side never reached us (the close-freeze pattern).
        from gabagent.voice.debuglog import dlog
        busy = _busy(vs)
        dlog(ctx, "respond_recv", session=sid, words=len(text.split()), busy=busy)
        # Any incoming utterance (addressed or aside) refreshes the duck watchdog, so a legitimate sustained
        # hold during dictation is never auto-restored — only genuine silence after an unreleased duck is.
        from gabagent.voice.ducking import note_duck_activity
        note_duck_activity(ctx)
        if busy:
            return JSONResponse(
                {"error": "a turn is already in progress for this session"}, status_code=409
            )
        start_turn(ctx, vs, text, wake=wake)
        if getattr(ctx.config, "voice_wake_arbiter_enabled", False):
            # Winner is now actually answering → mark its wake window so a stood-down peer's fallback stays
            # down. An UNMARKED window releases the peer (never-zero). Cheap flock touch; no-op if no window.
            # Keys on the same room id the voice side put in the /prewarm wake_claim.
            from gabagent.voice import wake_arbiter
            wake_arbiter.mark_answered(vs.room_id or sid)
        return _sse(drain(vs))

    async def confirm(request):
        body = await request.json()
        vs = sessions.get(body.get("session_id", ""))
        if vs is None:
            return JSONResponse({"ok": False, "error": "unknown session"}, status_code=404)
        if not _busy(vs):
            return JSONResponse({"ok": False, "error": "no turn awaiting confirmation"}, status_code=409)
        rid = _coerce_room_id(body.get("room_id"))   # informational here (confirm keys on session_id); kept fresh
        if rid is not None:
            vs.room_id = rid
        ctx.voice_session = vs
        ok = vs.resolve(body.get("id", ""), bool(body.get("approved")), body.get("passphrase"))
        if not ok:
            return JSONResponse({"ok": False, "error": "no matching pending confirmation"}, status_code=409)
        # The continuation streams back on THIS response (voice client's two-turn model).
        return _sse(drain(vs))

    async def cancel(request):
        body = await request.json()
        vs = sessions.get(body.get("session_id", ""))
        if vs is None:
            return JSONResponse({"ok": False, "error": "unknown session"}, status_code=404)
        # Log the barge-in receipt and whether a live turn was actually aborted — so a turn cancelled
        # mid-flight is traceable to the /cancel that killed it (e.g. a spurious empty barge-in), not a
        # mystery silent drop. Pairs with the `cancelled` dlog in the turn's CancelledError handler.
        from gabagent.voice.debuglog import dlog
        aborted = vs.turn_task is not None and not vs.turn_task.done()
        rid = _coerce_room_id(body.get("room_id"))   # informational here (cancel keys on session_id); kept fresh
        if rid is not None:
            vs.room_id = rid
        dlog(ctx, "cancel_recv", session=body.get("session_id", ""), aborted_live_turn=aborted)
        if aborted:
            vs.turn_task.cancel()
        vs.clear_pending(approved=False)
        return JSONResponse({"ok": True})

    async def attach(request):
        # Net-new capability handshake (STT-offload / multi-room foundation). The voice client calls this
        # ONCE after /health, before the first turn, to register its room + what it does locally. Bearer-
        # guarded like every non-/health endpoint (the middleware wraps this route automatically).
        #   POST {session_id, room_id, capabilities:{wake,vad,stt,tts}} -> {status, brain:{...}}
        # Properties (agreed in the seam ADR):
        #  - Idempotent: get-or-create the session, upsert room_id/capabilities; safe to re-call on a
        #    client reconnect or brain restart, never errors on repeat.
        #  - Tolerant: missing/garbage fields default rather than 400, so a newer client adding a
        #    capability key can't break an older brain.
        #  - Bidirectional: returns the brain's OWN capabilities so the client can negotiate instead of
        #    assuming. `accepts_room_id` advertises that payloads may carry the optional room key.
        body = await request.json()
        sid = body.get("session_id", "default")
        rid = _coerce_room_id(body.get("room_id"))
        caps = body.get("capabilities")
        if not isinstance(caps, dict):
            caps = {}
        vs = get_session(sid)
        if rid is not None:
            vs.room_id = rid
        vs.capabilities = caps
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "attach", session=sid, room=rid, caps=sorted(caps.keys()))
        from gabagent import __version__
        return JSONResponse(
            {"status": "ok", "brain": {"version": __version__, "accepts_room_id": True}}
        )

    async def media_duck(request):
        # Called on VAD speech-onset/end: duck music + movie volume so Aria can hear over playback.
        # `mute=True` (sent when the wake/command window opens) deepens the duck to a full mute (vol 0)
        # so the media's acoustic AEC residual can't leak a music vocal into a spurious USER turn.
        from gabagent.voice.ducking import duck_media
        body = await request.json()
        return JSONResponse(await duck_media(
            ctx, bool(body.get("on")), session_id=body.get("session_id"), mute=bool(body.get("mute"))))

    async def builder_poll(request):
        # The steady, SLEEP-INDEPENDENT proactive channel (phase 2): the voice side polls this on a fixed
        # cadence regardless of media/sleep to drain deferred spoken announcements (builder results, timer
        # rings, future nudges) — see voice/announce_store.py. Deliberately NOT the /media/state duck poll
        # (that GET is provider-neutral and dead when asleep; this one carries builder payloads and must run
        # while asleep). Two-phase, liveness-leased delivery: `session_id` is the poller's identity (claims
        # are renewed by continued polling, never a speak-deadline); `ack=<job_id>[,<id2>]` finalizes items
        # the caller just spoke (piggybacked on the next poll — no separate endpoint). Named /builder/poll
        # by consensus though it carries timer rings too: it is the general deferred-announce channel.
        from gabagent.voice import announce_store
        session_id = request.query_params.get("session_id", "") or ""
        for jid in (request.query_params.get("ack", "") or "").split(","):
            jid = jid.strip()
            if jid:
                announce_store.ack(jid)
        lease = float(getattr(ctx.config, "voice_announce_lease_secs", announce_store.DEFAULT_LEASE_SECS))
        return JSONResponse({"deferred": announce_store.poll(session_id, lease_secs=lease)})

    async def prewarm(request):
        # Cold-start mitigation (#2): the voice side fires this fire-and-forget the instant it detects the
        # FIRST post-wake voice energy — BEFORE STT — so a throwaway arya completion warms the cloud session
        # while the user is still speaking, overlapping arya's ~18-21s deep-cold spin with dead time the user
        # already spends. The brain has no earlier turn signal (it first hears a turn at /respond, AFTER STT),
        # which is why this seam exists. Idempotent + per-room rate-limited; the caller ignores the response.
        #
        # This same pre-STT seam ALSO carries the cross-room wake arbiter (Stage 2 of the double-answer fix)
        # when `voice_wake_arbiter_enabled` — a /prewarm with a `wake_claim` opens/joins a short grace window
        # and RETURNS a proceed|stand_down verdict the voice side honors before burning STT (a real round-trip
        # semantic, unlike the fire-and-forget warm). Disabled ⇒ this whole branch is skipped and behavior is
        # byte-identical to the warm-only path below. See wake_arbiter.py for the design.
        import time
        import asyncio as _aio
        from gabagent.api.models import ChatMessage
        from gabagent.voice.debuglog import dlog
        try:
            body = await request.json()
        except Exception:
            body = {}
        room = str(body.get("room") or body.get("session_id") or "default")

        async def _warm(client_ts):
            rt = getattr(ctx.config, "router", None)
            model = (getattr(rt, "simple_model", None) if rt else None) or "arya"
            t = time.monotonic()
            try:
                await ctx.client.complete_simple([ChatMessage(role="user", content="hi")], model=model)
                dlog(ctx, "prewarm", room=room, model=model,
                     ttft_ms=int((time.monotonic() - t) * 1000), client_ts=client_ts)
            except Exception as e:
                dlog(ctx, "prewarm", room=room, model=model, error=type(e).__name__)

        if getattr(ctx.config, "voice_wake_arbiter_enabled", False):
            from gabagent.voice import wake_arbiter
            # Liveness commit: the winner stamps it the instant it accepts `proceed` (before STT) so the
            # never-zero fallback keys on "did the winner START" (lands in ms) not "did it FINISH" (6-26s).
            commit_req = body.get("wake_commit")
            if isinstance(commit_req, dict):
                ok = wake_arbiter.mark_committed(str(commit_req.get("window_id") or ""), room)
                dlog(ctx, "wake_arbiter", room=room, phase="commit", committed=ok)
                return JSONResponse({"ok": True, "arbiter": {"committed": ok}})
            # Fallback probe: a stood-down room asking whether the winner actually took the turn.
            resolve_req = body.get("wake_resolve")
            if isinstance(resolve_req, dict):
                liveness = float(getattr(ctx.config, "voice_wake_arbiter_liveness_secs", 0.0))
                v = wake_arbiter.check_fallback(str(resolve_req.get("window_id") or ""), room,
                                                liveness_secs=liveness)
                dlog(ctx, "wake_arbiter", room=room, phase="resolve", verdict=v.get("verdict"))
                return JSONResponse({"ok": True, "arbiter": v})
            # Claim: register, hold until the grace window closes, then return the verdict.
            claim_req = body.get("wake_claim")
            if isinstance(claim_req, dict):
                window_secs = float(getattr(ctx.config, "voice_wake_arbiter_window_secs", 0.25))
                resolve_secs = float(getattr(ctx.config, "voice_wake_arbiter_resolve_secs", 2.5))
                c = wake_arbiter.claim(
                    room,
                    detector_latency_ms=float(claim_req.get("detector_latency_ms") or 0.0),
                    media_playing=bool(claim_req.get("media_playing")),
                    window_secs=window_secs,
                )
                wait = c["decide_at"] - time.time()  # async wait, NEVER under the store lock
                if wait > 0:
                    await _aio.sleep(wait)
                verdict = wake_arbiter.resolve(c["window_id"], room)
                v = verdict.get("verdict")
                if v == "pending":  # shouldn't happen (we waited past decide_at) — fail open
                    v = "proceed"
                dlog(ctx, "wake_arbiter", room=room, phase="claim", verdict=v,
                     winner=verdict.get("winner_room"), window=c["window_id"])
                out = {
                    "verdict": v,
                    "window_id": c["window_id"],
                    "claim_id": c["claim_id"],
                    "winner_room": verdict.get("winner_room"),
                    "resolve_after_ms": int(resolve_secs * 1000),
                }
                if v == "proceed" and getattr(ctx.config, "voice_prewarm_enabled", True):
                    _prewarm_last[room] = time.monotonic()  # dovetail the warm-only cooldown
                    _aio.create_task(_warm(body.get("ts")))
                return JSONResponse({"ok": True, "arbiter": out})
            # arbiter on but a plain warm-only /prewarm → fall through to the warm path below.

        if not getattr(ctx.config, "voice_prewarm_enabled", True):
            return JSONResponse({"ok": True, "skipped": "disabled"})
        cooldown = float(getattr(ctx.config, "voice_prewarm_cooldown_secs", 4.0))
        now = time.monotonic()
        if now - _prewarm_last.get(room, 0.0) < cooldown:
            return JSONResponse({"ok": True, "skipped": "cooldown"})
        _prewarm_last[room] = now
        _aio.create_task(_warm(body.get("ts")))
        return JSONResponse({"ok": True, "warming": True})

    async def media_state(request):
        # Read-only: lets the voice client's duck-timing skip ducking when nothing's playing.
        # PROTOCOL INVARIANT: this response (like every event/endpoint here) stays brain-agnostic —
        # provider-NEUTRAL, no jellyfin/tidal/etc. names cross to the voice side. Keep it generic so
        # any brain is pluggable. (Enforced by test_media_state_is_provider_neutral.)
        from gabagent.voice.ducking import media_state as _media_state, note_duck_activity
        # `bot_speaking=true` on this ~1 Hz poll means Aria's TTS is actively playing. A long reply (a story)
        # produces no INCOMING utterance, so the duck-watchdog — which times out from the last incoming voice
        # activity — would otherwise auto-restore the bed mid-reply (the live-drive #2 pop). Treat the poll as
        # a heartbeat REFRESH while she's speaking, so the watchdog only ever fires on genuine silence (still a
        # true crash-net for a stranded duck). Absent/`false` ⇒ a plain check, exactly as before (back-compat
        # with an older voice side). The check itself runs inside _media_state below, so refresh first.
        if request.query_params.get("bot_speaking") == "true":
            note_duck_activity(ctx)
        return JSONResponse(await _media_state(ctx))

    # A shared-secret bearer guard only when a token is configured (i.e. a LAN bind). Loopback default
    # leaves it off → zero behavior change. Added via Starlette's middleware= so build_app still returns
    # the Starlette instance (tests drive app.state.sessions directly).
    middleware = []
    token = (getattr(ctx.config, "voice_auth_token", "") or "").strip()
    if token:
        from starlette.middleware import Middleware
        middleware.append(Middleware(_BearerAuth, token=token))

    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/attach", attach, methods=["POST"]),
        Route("/respond", respond, methods=["POST"]),
        Route("/confirm", confirm, methods=["POST"]),
        Route("/cancel", cancel, methods=["POST"]),
        Route("/media/duck", media_duck, methods=["POST"]),
        Route("/media/state", media_state, methods=["GET"]),
        Route("/builder/poll", builder_poll, methods=["GET"]),
        Route("/prewarm", prewarm, methods=["POST"]),
    ], middleware=middleware)
    app.state.sessions = sessions
    return app


async def serve_voice(ctx: AgentContext, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        import uvicorn
        app = build_app(ctx)  # imports starlette internally
    except ImportError as e:  # pragma: no cover - exercised via CLI message
        raise RuntimeError(
            "Voice mode needs the optional extras. Install with: pip install gabagent[voice]"
        ) from e

    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", lifespan="off", loop="asyncio"
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # don't hijack SIGINT in the shared loop

    # Independent ~1 Hz timer-expiry loop (G2). Not tied to the /media/state poll so a timer set
    # in silence still fires. Cancelled on shutdown.
    import asyncio
    from gabagent.voice.timers import ticker as _timer_ticker
    timer_task = asyncio.create_task(_timer_ticker(ctx, app.state.sessions))

    # LAN discovery: advertise _voice-brain._tcp so a satellite finds this host without a typed IP.
    # Opt-in (`voice_advertise`, written True by the voice-host installer role) and additionally gated on a
    # non-loopback bind, fail-soft on absent zeroconf. The getattr default mirrors the schema default, so an
    # unconfigured install — or an older config object without the field — advertises nothing.
    # The advertised room falls back to the launch `--room-id` when `voice_room_id` is unset (per the maintainer, 2026-07-21):
    # a brain started `--room-id <room>` DOES serve a named room, so advertising "" was a dishonest TXT that a
    # room-filtering satellite could only match by accident. Config still wins when explicitly set.
    from gabagent.voice.advertiser import BrainAdvertiser
    advertiser = BrainAdvertiser()
    if getattr(ctx.config, "voice_advertise", False):
        advert_room = (getattr(ctx.config, "voice_room_id", "") or getattr(ctx, "room_id", "") or "").strip()
        await advertiser.start(host, port, room_id=advert_room)
    try:
        await server.serve()
    finally:
        timer_task.cancel()
        await advertiser.stop()
