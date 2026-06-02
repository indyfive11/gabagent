"""TUI-free turn runner for voice mode.

A turn runs as a persistent background task (`start_turn`) that emits VoiceEvents into
the session's queue. HTTP requests consume those events via `drain`, which stops at a
`confirm` (the SSE ends; the turn task stays suspended awaiting the decision) or at
`done` (turn complete). `POST /confirm` resolves the decision and a fresh `drain` streams
the continuation. This matches the voice client's two-turn confirm model — the post-confirm
continuation arrives on the /confirm response, not on the original /respond stream.

The turn mirrors agent/loop.py::run_loop's per-turn bookkeeping but emits structured
events instead of rendering a TUI, reusing `_execute_tool_calls` via ctx.approval_hook
rather than forking the tool loop.
"""
from __future__ import annotations
import asyncio
from typing import AsyncIterator, TYPE_CHECKING

from gabagent.api.models import ChatMessage
from gabagent.agent.loop import _execute_tool_calls, _active_client
from gabagent.voice import events
from gabagent.voice.events import VoiceEvent
from gabagent.voice.speakable import SpeakableFilter
from gabagent.voice import commands
from gabagent.voice.debuglog import dlog

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_WINDOW = 12  # recent non-system messages sent to the model

VOICE_ADDENDUM = (
    "You are speaking out loud through a voice assistant. Keep replies short and "
    "conversational — a sentence or two, no lists, headings, code blocks, or markdown. "
    "Don't read code or long file contents aloud — make the change and say one sentence "
    "about what you did. You can read files, search the web, and edit files in safe folders "
    "without asking. For edits to the current project you'll ask out loud to confirm. For "
    "risky actions like deleting files or running shell commands, say you need keyboard "
    "confirmation. You can control media and device functions when they're available — but only "
    "offer what's actually available: if unsure what you can control, check your capabilities first "
    "rather than promising. You can also say 'switch to local' or 'back to cloud' to change models."
)


class NullToolDisplay:
    def show_start(self, *a, **k): ...
    def show_result(self, *a, **k): ...


def _voice_system(ctx: AgentContext) -> str:
    s = VOICE_ADDENDUM + f"\n\nWorking directory: {ctx.cwd}"
    persona = (getattr(ctx.config, "voice_persona", "") or "").strip()
    if persona:
        s += f"\n\nStyle: speak as a {persona}."
    if ctx.local_mode and ctx.local_context_summary:
        s += f"\n\n{ctx.local_context_summary}"
    return s


def _build_voice_messages(ctx: AgentContext, messages: list[ChatMessage]) -> list[ChatMessage]:
    recent = [m for m in messages if m.role != "system"][-_WINDOW:]
    return [ChatMessage(role="system", content=_voice_system(ctx))] + recent


def _voice_tool_schemas() -> list[dict]:
    from gabagent.tools.registry import registry
    return [
        s for s in registry.get_schemas()
        if s["function"]["name"] not in ("bash", "run_shell")
    ]


def _status_phrase(tool_calls) -> str:
    names = {tc.name for tc in tool_calls}
    if names & {"write_file", "edit", "git_commit"}:
        return "Making that change."
    if "run_command" in names:
        domain = _run_command_domain(tool_calls)
        return f"Trying {domain}…" if domain else "Setting that up."
    if names & {"list_capabilities", "rescan_capabilities"}:
        return "Checking what I can control."
    if names & {"web_search", "web_fetch"}:
        return "Looking that up."
    if names & {"grep", "read_file", "glob"}:
        return "Reading through things."
    return "Looking into it."


def _run_command_domain(tool_calls) -> str:
    """Best-effort human domain for the first run_command (e.g. 'Jellyfin'). Empty if unknown."""
    import json
    for tc in tool_calls:
        if tc.name != "run_command":
            continue
        try:
            cid = (json.loads(tc.arguments) if tc.arguments else {}).get("command_id", "")
        except Exception:
            cid = ""
        if cid.startswith("jellyfin."):
            return "Jellyfin"
        if "." in cid:
            return cid.split(".", 1)[0]
    return ""


async def _emit_filtered(sfilter: SpeakableFilter, parts, emit) -> None:
    for kind, payload in parts:
        if kind == "speak":
            await emit(events.token(payload))
        else:
            await emit(events.status(payload))


async def _handle_meta(ctx: AgentContext, mc: commands.MetaCommand, emit) -> None:
    if mc.kind == "brain":
        if mc.value == "local":
            await emit(events.status(commands.filler("to_local", ctx)))
            err = await commands.switch_to_local(ctx)
            if err:
                await emit(events.token(f"I couldn't switch to local: {err}."))
            else:
                await emit(events.token(f"Okay, I'm on {ctx.config.local_model} now."))
        else:
            await emit(events.status(commands.filler("to_cloud", ctx)))
            await commands.switch_to_cloud(ctx)
            await emit(events.token("Okay, back on the cloud brain."))
    elif mc.kind == "undo":
        await emit(events.token(commands.undo_last(ctx)))
    elif mc.kind == "query":
        await emit(events.token(commands.answer_query(ctx, mc.value)))
    await emit(events.done())


async def _run_turn(ctx: AgentContext, vs, user_text: str) -> None:
    """Drive one turn, emitting VoiceEvents into vs.queue. Suspends naturally at a
    confirm (inside voice_approve's await) and resumes when /confirm resolves it.
    Always terminates the event stream with a `done`."""
    emit = ctx.voice_emit
    try:
        mc = commands.detect_meta_command(user_text)
        if mc is not None:
            dlog(ctx, "meta", matched=f"{mc.kind}:{mc.value}".rstrip(":"))
            await _handle_meta(ctx, mc, emit)
            return
        dlog(ctx, "meta", matched="none", routed="llm")

        ctx.session.append_message(ChatMessage(role="user", content=user_text))

        router = None
        if not ctx.force_model and not ctx.local_mode and ctx.config.router.enabled:
            from gabagent.agent.router import ModelRouter
            router = ModelRouter(ctx.config)

        simple = ctx.config.router.simple_model
        if router and ctx.active_model is None:
            try:
                ctx.active_model = await router.classify_intent(user_text, _active_client(ctx))
            except Exception:
                ctx.active_model = simple
            dlog(ctx, "route", active=ctx.active_model, via="intent_classify")
        prev_model = simple

        from gabagent.permissions.engine import PermissionEngine
        perm_engine = PermissionEngine(ctx.config)

        while True:
            cur = ctx.active_model or simple
            if not ctx.local_mode and cur != simple and cur != prev_model:
                await emit(events.status(commands.filler("escalate", ctx)))
                dlog(ctx, "switch", to=cur, via="escalation")
            prev_model = cur

            all_messages = _build_voice_messages(ctx, ctx.session.messages())
            tools = _voice_tool_schemas()
            request_model = None if ctx.local_mode else ctx.active_model
            sfilter = SpeakableFilter(code_notice=commands.filler("code", ctx))

            text_buf = ""
            tool_calls: list = []
            stream = _active_client(ctx).stream_complete(
                all_messages, tools or None, model=request_model
            )
            if ctx.local_mode:
                async for chunk in stream:
                    if isinstance(chunk, str):
                        text_buf += chunk
                    elif isinstance(chunk, list):
                        tool_calls = [tc for tc in chunk if tc.name]
                if not tool_calls and text_buf:
                    from gabagent.api.client import _extract_text_tool_calls
                    prose, parsed = _extract_text_tool_calls(text_buf)
                    if parsed:
                        tool_calls = parsed
                        text_buf = prose
                await _emit_filtered(sfilter, sfilter.feed(text_buf), emit)
            else:
                async for chunk in stream:
                    if isinstance(chunk, str):
                        text_buf += chunk
                        await _emit_filtered(sfilter, sfilter.feed(chunk), emit)
                    elif isinstance(chunk, list):
                        tool_calls = [tc for tc in chunk if tc.name]
            await _emit_filtered(sfilter, sfilter.flush(), emit)

            if text_buf:
                ctx.session.append_message(
                    ChatMessage(role="assistant", content=text_buf, tool_calls=tool_calls or None)
                )
            if not tool_calls:
                break
            if not text_buf:
                ctx.session.append_message(
                    ChatMessage(role="assistant", content=None, tool_calls=tool_calls)
                )

            await emit(events.status(_status_phrase(tool_calls)))
            results = await _execute_tool_calls(
                tool_calls, ctx, perm_engine, None, NullToolDisplay(), router
            )
            for tc, result in zip(tool_calls, results):
                dlog(ctx, "tool", name=tc.name, ok=result.success, error=result.error)
                ctx.session.append_message(
                    ChatMessage(role="tool", content=result.to_content(), tool_call_id=tc.id)
                )

        await emit(events.done())
    except asyncio.CancelledError:
        if vs.queue is not None:
            vs.queue.put_nowait(events.done())
        raise
    except Exception as e:
        cause = f"{type(e).__name__}: {e}".strip()
        # Persist a full traceback for diagnosis (mirrors the TUI crash path in cli.py).
        try:
            import traceback
            from gabagent.session.postmortem import PostMortemManager
            PostMortemManager(cwd=ctx.cwd).log_crash(
                f"Voice turn failed for: {user_text!r}\n\n{traceback.format_exc()}"
            )
        except Exception:
            pass
        dlog(ctx, "error", cause=cause, stage="turn")
        if vs is not None:
            vs.set_error(cause)
        if vs.queue is not None:
            # voice-agent speaks `error.text` and logs `summary`; the trailing `done`
            # closes the SSE. (It also suppresses any status right after an error.)
            vs.queue.put_nowait(events.error(
                cause, "Sorry, I hit a problem — ask me what went wrong for details."))
            vs.queue.put_nowait(events.done())


def start_turn(ctx: AgentContext, vs, user_text: str) -> "asyncio.Task":
    """Begin a turn: fresh event queue + background task. Returns the task."""
    vs.queue = asyncio.Queue()

    async def emit(ev: VoiceEvent) -> None:
        await vs.queue.put(ev)

    ctx.voice_emit = emit
    vs.turn_task = asyncio.create_task(_run_turn(ctx, vs, user_text))
    return vs.turn_task


async def drain(vs) -> AsyncIterator[VoiceEvent]:
    """Yield events from the current turn until it pauses at a confirm or ends at done."""
    q = vs.queue
    while True:
        ev = await q.get()
        yield ev
        if ev.type in ("confirm", "done"):
            return
