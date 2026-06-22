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
    try:
        await server.serve()
    finally:
        timer_task.cancel()
