"""Loopback HTTP+SSE brain-protocol server for voice mode.

Endpoints (bind 127.0.0.1 only):
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
        vs = get_session(sid)
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
        start_turn(ctx, vs, text)
        return _sse(drain(vs))

    async def confirm(request):
        body = await request.json()
        vs = sessions.get(body.get("session_id", ""))
        if vs is None:
            return JSONResponse({"ok": False, "error": "unknown session"}, status_code=404)
        if not _busy(vs):
            return JSONResponse({"ok": False, "error": "no turn awaiting confirmation"}, status_code=409)
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
        dlog(ctx, "cancel_recv", session=body.get("session_id", ""), aborted_live_turn=aborted)
        if aborted:
            vs.turn_task.cancel()
        vs.clear_pending(approved=False)
        return JSONResponse({"ok": True})

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

    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/respond", respond, methods=["POST"]),
        Route("/confirm", confirm, methods=["POST"]),
        Route("/cancel", cancel, methods=["POST"]),
        Route("/media/duck", media_duck, methods=["POST"]),
        Route("/media/state", media_state, methods=["GET"]),
    ])
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
