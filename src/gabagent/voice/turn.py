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
import json
import time
from typing import AsyncIterator, TYPE_CHECKING

from gabagent.api.models import ChatMessage
from gabagent.api.client import _is_transient_generation_error
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
    "conversational — ONE short sentence, no lists, headings, code blocks, or markdown. "
    "Don't add pleasantries or offers like 'let me know if you need anything else' or 'glad I "
    "could help' — just answer or report the result and stop. Keep casual conversation just as short: "
    "one sentence, no rambling or philosophizing. "
    "Don't read code or long file contents aloud — make the change and say one sentence "
    "about what you did. You can read files, search the web, and edit files in safe folders "
    "without asking. For edits to the current project you'll ask out loud to confirm. For "
    "risky actions like deleting files or running shell commands, say you need keyboard "
    "confirmation. You can control media and device functions when they're available — but only "
    "offer what's actually available: if unsure what you can control, check your capabilities first "
    "rather than promising. You can also say 'switch to local' or 'back to cloud' to change models. "
    "When you report what you did, describe only what the action actually guarantees — say 'I opened "
    "it in your browser', not that you changed a specific tab you can't target; never claim an outcome "
    "you can't verify. Never state what's currently playing or the current state of anything from "
    "memory or assumption — CHECK it first (e.g. now_playing) before answering, because something you "
    "started earlier may since have been paused or stopped; don't say music is playing unless you just "
    "verified it. When the user asks for several things in one breath (e.g. 'play some music and keep it "
    "quiet'), do every part and mention each one you did — don't report only the last action. To change "
    "how loud MUSIC is, use the music volume control, not the system volume. "
    "Don't invent reasons for being slow or for anything you can't actually explain — say "
    "you're not sure rather than making up a cause. If a tool returns an error or says it couldn't do something, tell the user it "
    "didn't work — never say you did it. You can take a screenshot, but you can't see or read images: "
    "if asked what's on the screen, say you can capture it but can't look at it — never make up what it "
    "shows or do an unrelated action instead. You can list the screens/monitors (list_screens) but not "
    "the names of individual open windows — if asked to list the windows, say so rather than moving one. "
    "You keep a growing personal memory of this project: save "
    "lasting facts with memory_write, and the user can ask what you remember or tell you to forget "
    "something. "
    "To stop or pause you, the user speaks to the voice layer directly (you never see these): "
    "'shut down voice mode' (or 'exit/quit voice mode') closes you completely, and 'go to sleep' "
    "(or 'stop listening') pauses you until they say 'wake up'. "
    "You can't shut yourself down, pause, mute, or sleep voice mode on your own — the voice layer "
    "controls that, not you. Phrase these in the FIRST PERSON ('I can't…', never 'yourself'). If the "
    "user wants to stop or turn you off completely, tell them to say 'shut down voice mode.' If they "
    "want you to mute, be quiet, pause listening, or only respond when called, that's the SLEEP "
    "control — tell them to say 'go to sleep' (or 'stop listening') and you'll ignore everything until "
    "they say 'wake up.' Never claim you did any of these yourself."
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


_HOT_CAP = 12


def _hot_command_ids(ctx, cat) -> list[str]:
    """The inline hot set: featured first-party core, then usage-promoted ids — capped. Flat as the
    skill count grows; frequently-used skills bubble up here without curation."""
    seen: list[str] = []
    for c in cat.featured():
        if c.id not in seen:
            seen.append(c.id)
    if len(seen) < _HOT_CAP:
        from gabagent.commands import usage
        for cid in usage.top(_HOT_CAP):
            if cid not in seen and cat.get(cid) is not None:
                seen.append(cid)
            if len(seen) >= _HOT_CAP:
                break
    return seen[:_HOT_CAP]


def _capability_brief(ctx: AgentContext) -> str:
    """A TIGHT, ~flat index — a per-domain overview plus a capped hot set of common commands — so the
    model knows the categories of what it can do without the full per-command manifest in context
    every turn. Details for the long tail are fetched on demand via list_capabilities. Scales to
    hundreds of skills; the model never denies a capability that fits a listed domain."""
    cat = getattr(ctx, "command_catalog", None)
    if cat is None:
        return ""
    idx = cat.index()
    if not idx:
        return ""
    from gabagent.voice import commands as _vc
    dom_lines = []
    for d in idx:
        desc = _vc._DOMAIN_PHRASE.get(d["domain"], "")
        dom_lines.append(f"- {d['domain']} ({d['count']})" + (f" — {desc}" if desc else ""))

    hot_ids = set(_hot_command_ids(ctx, cat))
    hot_lines = []
    for s in cat.summaries():
        if s["id"] in hot_ids:
            sig = ", ".join(_param_sig(p) for p in s["params"])
            summ = s["summary"][:60]
            # Render the id standalone with args labeled — NOT as `id(params)`, which the model misreads
            # as a callable function and tries to invoke directly (a real failure mode: it emitted
            # `jellyfin.search` as a tool name and the API rejected every turn).
            hot_lines.append(f"  - {s['id']} — {summ}" + (f"  · args: {sig}" if sig else ""))

    parts = [
        "Things you can do on this machine, by domain. These are NOT callable functions — invoke any of "
        "them with the run_command tool, passing the command_id and args. For the exact id and "
        "parameters of anything not under \"Common\", call list_capabilities(domain=… or query=…) FIRST "
        "— do NOT deny a capability that fits a listed domain; look it up:",
        *dom_lines,
    ]
    if hot_lines:
        parts += [
            "Common command_ids (pass one as run_command's command_id — do NOT call them as functions):",
            *hot_lines,
        ]
    return "\n".join(parts)


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


def _looks_simple(text: str) -> bool:
    """Voice-only fast-path: obviously-simple utterances skip the classifier's API round-trip and run
    on the base model. Conservative — returns False (→ classify) when unsure, so genuinely complex
    requests still escalate."""
    t = text.strip().lower()
    if not t:
        return True
    # Short, single-clause commands ("stop", "pause the movie", "volume up") are simple.
    return len(t.split()) <= 6 and " and " not in f" {t} "


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
    # Turn lifecycle markers: a `turn_start` with no matching `turn_done` is the signature of a hang
    # (model loop, tool, or page-eval) that left no other trace — the close-movie freeze had none of these.
    _t0 = time.monotonic()
    dlog(ctx, "turn_start", words=len(user_text.split()))
    try:
        mc = commands.detect_meta_command(user_text)
        if mc is not None:
            dlog(ctx, "meta", matched=f"{mc.kind}:{mc.value}".rstrip(":"))
            await _handle_meta(ctx, mc, emit)
            return
        dlog(ctx, "meta", matched="none", routed="llm")

        # "Addressed-to-me?" filter: the wake window passes follow-on speech for multi-part commands,
        # so undirected speech (a curse, thinking aloud, commentary about the assistant) can land here
        # and otherwise get a spoken reply it shouldn't. If this utterance isn't directed at the
        # assistant, emit nothing — no reply, no action, no history append — and close the turn. The
        # gate stays the wake authority; this is complementary, catching what slips through an open
        # (or false-wake) window. Conservative: answers when unsure, so it never eats a real command.
        if getattr(ctx.config, "voice_intent_filter", True):
            from gabagent.voice.addressed import is_addressed
            addressed, via = await is_addressed(ctx, user_text)
            dlog(ctx, "addressed", match=addressed, via=via)
            if not addressed:
                await emit(events.done())
                return

        ctx.session.append_message(ChatMessage(role="user", content=user_text))

        router = None
        if not ctx.force_model and not ctx.local_mode and ctx.config.router.enabled:
            from gabagent.agent.router import ModelRouter
            router = ModelRouter(ctx.config)

        is_claude = ctx.config.provider == "claude"
        # Baseline "cheap" rung for this provider: arya on gab, the bottom ladder rung on Claude.
        simple = ctx.config.claude.ladder[0].model if is_claude else ctx.config.router.simple_model
        simple_effort = (ctx.config.claude.ladder[0].effort or None) if is_claude else None
        if router:
            # Re-evaluate routing EACH turn so simple follow-ups drop back to the cheap rung instead of
            # pinning the whole session to the premium model. Obvious-simple utterances skip the
            # classifier's API round-trip; only substantive prompts pay for classification.
            if _looks_simple(user_text):
                ctx.active_model = simple
                ctx.active_effort = simple_effort
            elif is_claude:
                try:
                    rung = router.rung(await router.classify_rung(user_text, _active_client(ctx)))
                    ctx.active_model = rung.model
                    ctx.active_effort = rung.effort or None
                except Exception:
                    ctx.active_model = simple
                    ctx.active_effort = simple_effort
            else:
                try:
                    ctx.active_model = await router.classify_intent(user_text, _active_client(ctx))
                except Exception:
                    ctx.active_model = simple
            dlog(ctx, "route", active=ctx.active_model, effort=ctx.active_effort, via="intent_classify")

        from gabagent.permissions.engine import PermissionEngine
        perm_engine = PermissionEngine(ctx.config)

        last_status = None
        status_emitted = False   # cap spoken status to ONE per turn (no "Opening… Trying… Looking…" chains)
        fell_back = False        # turn-level arya fallback fires at most once per turn
        while True:
            cur = ctx.active_model or simple
            # Announce an arya→premium transition ONCE (vs. every turn), and de-escalate silently.
            announced = getattr(ctx, "voice_announced_model", None) or simple
            if not ctx.local_mode and cur != simple and cur != announced:
                await emit(events.status(commands.filler("escalate", ctx)))
                dlog(ctx, "switch", to=cur, via="escalation")
            ctx.voice_announced_model = cur

            all_messages = _build_voice_messages(ctx, ctx.session.messages())
            tools = _voice_tool_schemas()
            request_model = None if ctx.local_mode else ctx.active_model
            request_effort = None if ctx.local_mode else ctx.active_effort
            # If the model bungles a tool call (e.g. calls a command-id as a function), the client
            # retries on the stronger model — but only for that failed turn, so simple turns stay cheap.
            # (gab-only nudge; Claude has no command-id parse-error path.)
            retry_model = None if (ctx.local_mode or is_claude) else ctx.config.router.complex_model
            # If an escalated turn keeps failing to generate, fall back to the cheap rung rather than
            # erroring — a simpler answer beats a dead turn.
            fallback_model = None if ctx.local_mode else simple
            sfilter = SpeakableFilter(code_notice=commands.filler("code", ctx))

            text_buf = ""
            tool_calls: list = []
            stream = _active_client(ctx).stream_complete(
                all_messages, tools or None, model=request_model, effort=request_effort,
                retry_model=retry_model, fallback_model=fallback_model,
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
                try:
                    async for chunk in stream:
                        if isinstance(chunk, str):
                            text_buf += chunk
                            await _emit_filtered(sfilter, sfilter.feed(chunk), emit)
                        elif isinstance(chunk, list):
                            tool_calls = [tc for tc in chunk if tc.name]
                except Exception as e:
                    # An escalated turn that fails to generate (gab.ai 'inference_failed') falls back to
                    # the simple model at the TURN level — a guaranteed arya answer beats a spoken error.
                    # Only safe before any speech was emitted (else we'd replay it), and only once.
                    if (not text_buf and not fell_back and cur != simple
                            and _is_transient_generation_error(e)):
                        fell_back = True
                        ctx.active_model = simple
                        ctx.active_effort = simple_effort
                        ctx.voice_announced_model = simple   # don't announce the switch back
                        dlog(ctx, "fallback", to=simple, reason="inference_failed")
                        continue
                    raise
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
            if phrase and not status_emitted:   # one status per turn — no stacked preambles
                await emit(events.status(phrase))
                status_emitted = True
                last_status = phrase
            results = await _execute_tool_calls(
                tool_calls, ctx, perm_engine, None, NullToolDisplay(), router
            )
            for tc, result in zip(tool_calls, results):
                # Record the run_command command_id so the debug log shows WHICH capability ran each
                # turn (was inferred-by-tier before) — e.g. to see exactly what a "move the window" turn
                # invoked, or whether a control command paused a movie.
                cid = None
                if tc.name == "run_command":
                    try:
                        cid = (json.loads(tc.arguments) if tc.arguments else {}).get("command_id") or None
                    except Exception:
                        cid = None
                dlog(ctx, "tool", name=tc.name, command_id=cid, ok=result.success, error=result.error)
                ctx.session.append_message(
                    ChatMessage(role="tool", content=result.to_content(), tool_call_id=tc.id)
                )

        await emit(events.done())
    except asyncio.CancelledError:
        if vs.queue is not None:
            vs.queue.put_nowait(events.done())
        raise
    except Exception as e:
        # Last-ditch G6 fallback (the reliable one — the in-loop guard wasn't catching the
        # escalate-after-tool path live). An escalated turn that died on a transient gab.ai
        # 'inference_failed' gets ONE plain arya narration of the current state — no tools, no
        # re-execution (the file/tool already ran) — instead of speaking "[gab.ai error]".
        _simple = (ctx.config.claude.ladder[0].model
                   if ctx.config.provider == "claude" else ctx.config.router.simple_model)
        if (not ctx.local_mode and vs.queue is not None
                and (ctx.active_model or _simple) != _simple
                and _is_transient_generation_error(e)):
            try:
                ctx.active_model = _simple
                ctx.voice_announced_model = _simple
                msgs = _build_voice_messages(ctx, ctx.session.messages())
                text = await ctx.client.complete_simple(msgs, model=_simple)
                if text and text.strip():
                    dlog(ctx, "fallback", to=_simple, reason="inference_failed", level="turn")
                    sf = SpeakableFilter(code_notice=commands.filler("code", ctx))
                    await _emit_filtered(sf, sf.feed(text), emit)
                    await _emit_filtered(sf, sf.flush(), emit)
                    ctx.session.append_message(ChatMessage(role="assistant", content=text))
                    vs.queue.put_nowait(events.done())
                    return
            except Exception:
                pass  # arya also failed → fall through to the graceful error below
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
                cause, "Sorry, I had a brief hiccup — could you say that again?"))
            vs.queue.put_nowait(events.done())
    finally:
        # Fires on EVERY exit (meta return, normal done, cancel, error, fallback) — so the absence of a
        # turn_done after a turn_start unambiguously localises a hang to this turn.
        dlog(ctx, "turn_done", dur_ms=int((time.monotonic() - _t0) * 1000))


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
