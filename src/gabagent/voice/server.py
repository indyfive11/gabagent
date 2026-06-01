"""Loopback HTTP+SSE brain-protocol server for voice mode.

Endpoints (bind 127.0.0.1 only):
  GET  /health   -> {"status":"ok","mode":"voice"}
  POST /respond  -> SSE stream of VoiceEvents driving voice_turn
  POST /confirm  -> resolve a paused confirmation
  POST /cancel   -> abort the in-flight turn (barge-in)

Starlette + uvicorn are an OPTIONAL dependency ([voice] extra); imported lazily so the
base install never breaks. uvicorn runs embedded in the existing asyncio loop via
Server.serve() (never uvicorn.run()), with its signal handlers suppressed.
"""
from __future__ import annotations
import asyncio
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
    from gabagent.voice.turn import voice_turn

    sessions: dict[str, VoiceSession] = {}
    turn_lock = asyncio.Lock()  # Phase 1: one active turn at a time (single shared ctx)

    def get_session(sid: str) -> VoiceSession:
        vs = sessions.get(sid)
        if vs is None:
            vs = VoiceSession(sid, ctx, ctx.voice_audit_path)
            sessions[sid] = vs
        return vs

    async def health(request):
        return JSONResponse({"status": "ok", "mode": "voice"})

    async def respond(request):
        body = await request.json()
        sid = body.get("session_id", "default")
        text = body.get("text", "")
        vs = get_session(sid)

        async def gen():
            async with turn_lock:
                ctx.voice_session = vs
                async for ev in voice_turn(ctx, text):
                    yield ev.sse()

        return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def confirm(request):
        body = await request.json()
        vs = sessions.get(body.get("session_id", ""))
        if vs is None:
            return JSONResponse({"ok": False, "error": "unknown session"}, status_code=404)
        ok = vs.resolve(body.get("id", ""), bool(body.get("approved")), body.get("passphrase"))
        return JSONResponse({"ok": ok})

    async def cancel(request):
        body = await request.json()
        vs = sessions.get(body.get("session_id", ""))
        if vs is None:
            return JSONResponse({"ok": False, "error": "unknown session"}, status_code=404)
        vs.clear_pending(approved=False)
        if vs.active_task is not None and not vs.active_task.done():
            vs.active_task.cancel()
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/respond", respond, methods=["POST"]),
        Route("/confirm", confirm, methods=["POST"]),
        Route("/cancel", cancel, methods=["POST"]),
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
    await server.serve()
