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
    "rather than promising. You can also say 'switch to local' or 'back to cloud' to change models. "
    "When you report what you did, describe only what the action actually guarantees — say 'I opened "
    "it in your browser', not that you changed a specific tab you can't target; never claim an outcome "
    "you can't verify. You keep a growing personal memory of this project: save lasting facts with "
    "memory_write, and the user can ask what you remember or tell you to forget something. "
    "To stop or pause you, the user speaks to the voice layer directly (you never see these): "
    "'shut down voice mode' (or 'exit/quit voice mode') closes you completely, and 'go to sleep' "
    "(or 'stop listening') pauses you until they say 'wake up'. If asked how to stop or pause you, "
    "tell them those phrases."
)


class NullToolDisplay:
    def show_start(self, *a, **k): ...
    def show_result(self, *a, **k): ...


def _voice_system(ctx: AgentContext) -> str:
    s = VOICE_ADDENDUM + f"\n\nWorking directory: {ctx.cwd}"
    caps = _capability_brief(ctx)
    if caps:
        s += f"\n\n{caps}"
    mem = _memory_brief(ctx)
    if mem:
        s += f"\n\n{mem}"
    persona = (getattr(ctx.config, "voice_persona", "") or "").strip()
    if persona:
        s += f"\n\nStyle: speak as a {persona}."
    if ctx.local_mode and ctx.local_context_summary:
        s += f"\n\n{ctx.local_context_summary}"
    return s


def _param_sig(p: dict) -> str:
    """Render one slot as name[=enum|values][?] — ? marks an optional slot."""
    s = p["name"]
    if p.get("enum"):
        s += "=" + "|".join(str(v) for v in p["enum"])
    if not p.get("required"):
        s += "?"
    return s


def _capability_brief(ctx: AgentContext) -> str:
    """Ground the model in exactly what it can do on THIS host AND how to call it, so it never
    denies a real capability or invents an id. Built live from the catalog (providers + installed
    skills), grouped by domain, every turn — so newly installed skills appear automatically."""
    cat = getattr(ctx, "command_catalog", None)
    if cat is None:
        return ""
    rows = cat.summaries()  # sorted by id: {id, domain, summary, tier, params:[{name,type,required,enum?}]}
    if not rows:
        return ""
    by_domain: dict[str, list[str]] = {}
    for r in rows:
        sig = ", ".join(_param_sig(p) for p in r["params"])
        summ = r["summary"]
        if len(summ) > 72:
            summ = summ[:72] + "…"
        by_domain.setdefault(r["domain"], []).append(f"  - {r['id']}({sig}) — {summ}")
    lines: list[str] = []
    for dom in sorted(by_domain):
        lines.append(f"[{dom}]")
        lines.extend(by_domain[dom])
    return ("You can do these on this machine RIGHT NOW by calling run_command(command_id, args) with "
            "the exact ids and parameters below (params marked ? are optional). Do NOT deny a listed "
            "capability or invent ids that aren't here. If something you need isn't listed, say so or "
            "call rescan_capabilities:\n" + "\n".join(lines))


def _memory_brief(ctx: AgentContext, limit: int = 1500) -> str:
    """Your own saved notes about this project, so memory carries across sessions."""
    try:
        from gabagent.session.memory import MemoryManager
        mem = MemoryManager(ctx.cwd).load().strip()
    except Exception:
        mem = ""
    if not mem:
        return ""
    if len(mem) > limit:
        mem = "…" + mem[-limit:]
    return f"What you remember about this project (your own saved notes):\n{mem}"


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
    # Pure recon (list/rescan capabilities) is internal — don't narrate it; it's what produced the
    # noisy "Checking what I can control. Trying Jellyfin…" stacking.
    if names <= {"list_capabilities", "rescan_capabilities"}:
        return ""
    if names & {"write_file", "edit", "git_commit"}:
        return "Making that change."
    if "run_command" in names:
        domain = _run_command_domain(tool_calls)
        return f"Trying {domain}…" if domain else "Setting that up."
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
    elif mc.kind == "forget":
        await emit(events.token(commands.forget(ctx, mc.value)))
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

        last_status = None
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

            phrase = _status_phrase(tool_calls)
            if phrase and phrase != last_status:   # skip recon-only batches; dedup brain-side
                await emit(events.status(phrase))
                last_status = phrase
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
