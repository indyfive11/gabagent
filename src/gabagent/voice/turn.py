"""TUI-free single-turn runner for voice mode.

`voice_turn(ctx, user_text)` is an async generator of VoiceEvent objects. It mirrors
the per-turn bookkeeping of agent/loop.py::run_loop but emits structured events instead
of rendering to a TUI, reusing `_execute_tool_calls` (via the shared ctx.approval_hook)
rather than forking the tool loop. A background `drive` task runs the turn while the
generator yields from a queue, so tool-time events can interleave with the token stream.
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
    "confirmation. You can also say 'switch to local' or 'back to cloud' to change models."
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
    return "Looking into it."


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


async def voice_turn(ctx: AgentContext, user_text: str) -> AsyncIterator[VoiceEvent]:
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(ev: VoiceEvent) -> None:
        await queue.put(ev)

    ctx.voice_emit = emit

    async def drive() -> None:
        try:
            mc = commands.detect_meta_command(user_text)
            if mc is not None:
                await _handle_meta(ctx, mc, emit)
                return

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
            prev_model = simple

            from gabagent.permissions.engine import PermissionEngine
            perm_engine = PermissionEngine(ctx.config)

            while True:
                cur = ctx.active_model or simple
                if not ctx.local_mode and cur != simple and cur != prev_model:
                    await emit(events.status(commands.filler("escalate", ctx)))
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
                    ctx.session.append_message(
                        ChatMessage(role="tool", content=result.to_content(), tool_call_id=tc.id)
                    )

            await emit(events.done())
        except asyncio.CancelledError:
            try:
                await emit(events.done())
            except Exception:
                pass
        except Exception:
            await emit(events.status("Sorry, I hit an error and stopped."))
            await emit(events.done())
        finally:
            await queue.put(None)

    task = asyncio.create_task(drive())
    if ctx.voice_session is not None:
        ctx.voice_session.active_task = task
    try:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
    finally:
        if not task.done():
            task.cancel()
        if ctx.voice_session is not None:
            ctx.voice_session.active_task = None
            ctx.voice_session.clear_pending()
