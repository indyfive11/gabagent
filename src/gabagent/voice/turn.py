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
from gabagent.api.client import (
    _is_transient_generation_error, _is_hard_backend_error, _is_network_timeout,
)
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
    "Don't narrate what you're about to do or your reasoning ('let me check…', 'I need to search "
    "for…', 'one moment while I…') — just do it and give the result in one short clause; this matters "
    "most while a movie or music is playing, where the user wants a quick 'Done.' not a play-by-play. "
    "Your own name (Aria) at the start or end of a request is the user addressing you, not part of a "
    "movie or song title — ignore it when searching or playing (e.g. 'play the movie, Aria' means play "
    "the movie, not find a title called 'Aria'). "
    "When a tool reports the user cancelled or backed out of an action (e.g. a cancelled play "
    "confirmation), treat it as a deliberate, completed choice — acknowledge it briefly and ask what "
    "they'd like instead. Never describe a cancel as a failure or an error, never say it 'didn't "
    "work' or 'didn't start', and don't offer to retry the same thing. "
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
    "verified it. A track being loaded is NOT proof the user can hear it: if now_playing reports "
    "audible:false, tell them the music doesn't look audible and give the reason it returns (e.g. the "
    "output is muted or routed elsewhere) — do NOT claim it's playing or tell them to check their own "
    "sound settings. now_playing covers EVERY Jellyfin device, not just this machine — to answer whether a "
    "movie is still on, paused, or playing in another room or on the TV, read now_playing and name the "
    "device and exactly what it reports; never guess another device's state, and don't say nothing is "
    "playing until now_playing confirms it. When the user asks for several things in one breath (e.g. 'play some music and keep it "
    "quiet'), do every part and mention each one you did — don't report only the last action. To change "
    "how loud MUSIC is, use the music volume control, not the system volume. "
    "When the user loosely asks for music ('play something retro', 'put on a playlist', 'play some "
    "music'), just PICK a fitting option and play it right away — don't read back a list of choices and "
    "ask which one unless they explicitly ask what's available. If you just offered choices and the user "
    "replies with a single title or name ('Gunship'), that's their pick — play it immediately, don't ask "
    "again. "
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
    if getattr(ctx.config, "persona_enabled", True):
        pb = _persona_brief(ctx)
        if pb:
            s += f"\n\nThis is who you are (your personality — speak in character, never mention it):\n{pb}"
    elif (persona := (getattr(ctx.config, "voice_persona", "") or "").strip()):
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


def _persona_brief(ctx: AgentContext) -> str:
    """Your evolving personality (seed + traits learned with this user). Invisible to the user."""
    try:
        from gabagent.persona.manager import PersonaManager
        return PersonaManager().brief()
    except Exception:
        return ""


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


# Loop guard: when the model keeps firing the SAME tool call with no progress (live 2026-06-20:
# tidal.play x13 in one turn, each a no-op "Resuming.", the user heard nothing), don't spin. After a
# signature repeats this many times we step ONE rung up the ladder — least-escalation-first — to give a
# more tool-capable model a shot, and bail with an honest line if we're already at the top or it keeps
# looping. _MAX_TOOL_ROUNDS is the absolute per-turn backstop so no turn can hammer indefinitely.
_LOOP_REPEAT = 3
_MAX_TOOL_ROUNDS = 16
_LOOP_GIVEUP_MSG = "I'm having trouble getting that to work right now."


def _tool_sig(tc, result=None) -> str:
    """A signature for loop detection: the run_command command_id when present (else tool+args), folded
    with the RESULT text. "Stuck" means the SAME call returns the SAME response over and over (the live
    13x no-op "Resuming."); folding in the result means a call that makes PROGRESS — a different track, a
    disambiguation, an eventual success — gets a distinct signature, so a model working its way to an
    answer isn't mistaken for a stuck loop and told it failed right after it succeeded."""
    if tc.name == "run_command":
        try:
            cid = (json.loads(tc.arguments) if tc.arguments else {}).get("command_id")
        except Exception:
            cid = None
        base = f"run_command:{cid}" if cid else f"run_command:{tc.arguments or ''}"
    else:
        base = f"{tc.name}:{tc.arguments or ''}"
    if result is not None:
        out = (result.output or result.error or "").strip()[:80]
        base = f"{base}|{out}"
    return base


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


def _is_media_command(ctx: AgentContext, cid: str | None) -> bool:
    """True if `cid` is a media-domain command (jellyfin/tidal/mpris play/pause/seek/volume/…). Used to
    fire the media-control keepalive so follow-up commands don't get wake-gated out mid-interaction."""
    if not cid:
        return False
    cat = getattr(ctx, "command_catalog", None)
    if cat is None:
        return False
    cmd = cat.get(cid)
    return bool(cmd is not None and getattr(cmd, "domain", "") == "media")


async def _emit_filtered(sfilter: SpeakableFilter, parts, emit) -> None:
    for kind, payload in parts:
        if kind == "speak":
            await emit(events.token(payload))
        else:
            await emit(events.status(payload))


_BACKEND_NAME = {"gab": "Aria", "claude": "Claude", "local": "the local model"}


def _pin_friendly(model: str) -> str:
    m = model.lower()
    if "opus" in m: return "Opus"
    if "sonnet" in m: return "Sonnet"
    if "haiku" in m: return "Haiku"
    if m == "arya": return "Aria"
    return model


def _degraded_notice(failed: str, recovered: str) -> str:
    return (f"Heads up — {_BACKEND_NAME.get(failed, failed)} is unavailable "
            f"(looks like a billing or auth problem). I've switched to "
            f"{_BACKEND_NAME.get(recovered, recovered)} for now.")


def _reassemble_floor(ctx: AgentContext):
    """The floor rung of the ladder re-assembled with the current degraded set, as
    (model, effort, backend) — or None when nothing usable is left."""
    from gabagent.agent.router import ModelRouter
    r = ModelRouter.assemble(
        ctx.config, local_floor=ctx.local_floor,
        local_running=ctx.local_client is not None, degraded=ctx.degraded_backends,
    )
    if r and r.ladder:
        f = r.ladder[0]
        return (f.model, f.effort or None, f.backend)
    return None


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
    elif mc.kind == "floor":
        if mc.value == "local":
            await emit(events.status(commands.filler("to_local", ctx)))
            from gabagent.local.ollama import start_local_floor
            err = await start_local_floor(ctx)
            if err:
                await emit(events.token(f"I couldn't bring local up as the floor: {err}."))
            else:
                await emit(events.token(
                    f"Local's the floor now — {ctx.config.local_model}, escalating to Aria and Claude as needed."))
        else:
            from gabagent.local.ollama import stop_local_floor
            await stop_local_floor(ctx)
            await emit(events.token("Aria's the floor now — local's unloaded."))
    elif mc.kind == "pin":
        if mc.value == "auto":
            ctx.force_model = False
            ctx.pinned_model = ctx.pinned_effort = ctx.pinned_backend = None
            ctx.active_model = ctx.active_effort = ctx.active_backend = None
            await emit(events.token("Back to automatic — I'll pick the model per request."))
        else:
            from gabagent.agent.router import resolve_pin
            res = resolve_pin(mc.value, ctx.config)
            if res is None:
                await emit(events.token(f"I don't know the model {mc.value}."))
            else:
                model, effort, backend = res
                if backend == "local" and ctx.local_client is None:
                    from gabagent.local.ollama import start_local_floor
                    err = await start_local_floor(ctx, prewarm=False)
                    if err:
                        await emit(events.token(f"I couldn't start local: {err}."))
                        await emit(events.done())
                        return
                ctx.force_model = True
                ctx.pinned_model, ctx.pinned_effort, ctx.pinned_backend = model, effort, backend
                eff = f" at {effort}" if effort else ""
                await emit(events.token(
                    f"Locked to {_pin_friendly(model)}{eff} — say 'back to auto' to re-enable escalation."))
    elif mc.kind == "quiet":
        # "Shut up / be quiet": the brain can't mute or sleep itself (the voice layer owns that), so
        # answer with ONE short pointer instead of routing to the model — a terse line ends fast and,
        # while media plays, releases the duck quickly instead of holding it for a multi-sentence reply.
        await emit(events.token("Say \"go to sleep\" if you want me to stop listening."))
    elif mc.kind == "undo":
        await emit(events.token(commands.undo_last(ctx)))
    elif mc.kind == "forget":
        await emit(events.token(commands.forget(ctx, mc.value)))
    elif mc.kind == "query":
        await emit(events.token(commands.answer_query(ctx, mc.value)))
    await emit(events.done())


def _is_terminal_reply(text: str) -> bool:
    """Heuristic for a TERMINAL turn (for the conversation-hold release hint): a self-contained spoken
    reply with no expected follow-up. Conservative — a reply that ENDS in a question (a clarification,
    an option list, 'anything else?') is NOT terminal, so the voice-side hold stays open for the answer.
    Trailing quotes/emphasis/brackets are stripped before the final-punctuation check."""
    t = (text or "").strip()
    if not t:
        return False
    return not t.rstrip("\"')]>*_` ").endswith("?")


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
                # A1 movie-duck release: tell the voice client this turn is an aside the instant the
                # classifier returns, so a movie duck its VAD-onset pre-duck opened can be released now
                # rather than lingering until the voice-side 8s idle grace (~13s of the movie sitting at
                # 18% on ambient speech — the bug Rob dictated 2026-06-14). Emitted only on suppression;
                # the client treats addressed:true as a no-op. This is the earliest possible point.
                await emit(events.addressed(False))
                await emit(events.done())
                return

        ctx.session.append_message(ChatMessage(role="user", content=user_text))

        ctx.active_backend = None
        router = None
        if not ctx.force_model and not ctx.local_mode and ctx.config.router.enabled:
            from gabagent.agent.router import ModelRouter
            router = ModelRouter.assemble(
                ctx.config,
                local_floor=ctx.local_floor,
                local_running=ctx.local_client is not None,
                degraded=ctx.degraded_backends,
            ) or ModelRouter(ctx.config)

        assembled = bool(router and router.assembled)
        is_claude = ctx.config.provider == "claude"
        # Baseline FLOOR rung for this turn. Assembled: ladder[0] (local if running, else Aria).
        # Legacy: arya on gab, the bottom Claude rung on the claude provider.
        if assembled:
            floor = router.ladder[0]
            simple, simple_effort, simple_backend = floor.model, floor.effort or None, floor.backend
        else:
            simple = ctx.config.claude.ladder[0].model if is_claude else ctx.config.router.simple_model
            simple_effort = (ctx.config.claude.ladder[0].effort or None) if is_claude else None
            simple_backend = "claude" if is_claude else "gab"
        if router:
            # Re-evaluate routing EACH turn so simple follow-ups drop back to the cheap floor instead of
            # pinning the session to a premium rung. Obvious-simple utterances skip the classifier.
            if _looks_simple(user_text):
                ctx.active_model, ctx.active_effort, ctx.active_backend = simple, simple_effort, simple_backend
            elif assembled:
                try:
                    rung = router.rung(await router.classify_rung(
                        user_text, _active_client(ctx, router.classifier_backend)))
                    ctx.active_model, ctx.active_effort, ctx.active_backend = (
                        rung.model, rung.effort or None, rung.backend)
                except Exception:
                    ctx.active_model, ctx.active_effort, ctx.active_backend = simple, simple_effort, simple_backend
            elif is_claude:
                try:
                    rung = router.rung(await router.classify_rung(user_text, _active_client(ctx)))
                    ctx.active_model, ctx.active_effort, ctx.active_backend = (
                        rung.model, rung.effort or None, "claude")
                except Exception:
                    ctx.active_model, ctx.active_effort, ctx.active_backend = simple, simple_effort, "claude"
            else:
                try:
                    ctx.active_model = await router.classify_intent(user_text, _active_client(ctx))
                except Exception:
                    ctx.active_model = simple
                ctx.active_backend = "gab"
            dlog(ctx, "route", active=ctx.active_model, effort=ctx.active_effort,
                 backend=ctx.active_backend, via="intent_classify")

        # Device/media CONTROL turns drive run_command, where a weak model fumbles the tool protocol
        # (wrong arg shape / command id) rather than the reasoning — something the complexity classifier
        # never escalates for (it rates a short imperative as simple). Arm such turns for the reactive
        # sequential climb in the tool loop below: start at the floor (least escalation) and step ONE
        # rung up only on evidence of trouble. A LOCAL floor is the exception — a local model can't drive
        # tools at all, so don't waste a round discovering it; pre-bump to the first non-local rung.
        from gabagent.agent.router import looks_like_command
        command_intent = looks_like_command(user_text)
        if command_intent and router and assembled and ctx.active_backend == "local":
            nl = router.rung(1)  # first rung above the local floor (Aria, else Claude)
            if nl.backend != "local":
                ctx.active_model, ctx.active_effort, ctx.active_backend = (
                    nl.model, nl.effort or None, nl.backend)
                dlog(ctx, "route", active=ctx.active_model, backend=ctx.active_backend,
                     via="command_escalate")

        # Runtime model PIN: routing is skipped (force_model); run every turn on the pinned rung.
        if ctx.force_model and ctx.pinned_backend:
            ctx.active_model, ctx.active_effort, ctx.active_backend = (
                ctx.pinned_model, ctx.pinned_effort, ctx.pinned_backend)
            dlog(ctx, "route", active=ctx.active_model, effort=ctx.active_effort,
                 backend=ctx.active_backend, via="pinned")

        from gabagent.permissions.engine import PermissionEngine
        perm_engine = PermissionEngine(ctx.config)

        last_status = None
        status_emitted = False   # cap spoken status to ONE per turn (no "Opening… Trying… Looking…" chains)
        fell_back = False        # turn-level arya fallback fires at most once per turn
        media_ran = False        # any media-domain command this turn → emit a keepalive before `done`
        sig_counts: dict[str, int] = {}   # tool-call signature → times seen this turn (loop detector)
        tool_rounds = 0                    # total tool rounds this turn (hard backstop)
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
            # (gab-only nudge; Claude has no parse-error path, and the assembled ladder handles
            # escalation/fallback at the turn level across backends — a per-call retry would target the
            # wrong client.)
            retry_model = (
                None if (ctx.local_mode or is_claude or assembled or ctx.force_model)
                else ctx.config.router.complex_model
            )
            # Per-call fallback is single-backend; in assembled mode the turn-level fallback below
            # re-routes to the floor (and the floor's client) instead. A PIN stays on its model.
            fallback_model = None if (ctx.local_mode or assembled or ctx.force_model) else simple
            sfilter = SpeakableFilter(code_notice=commands.filler("code", ctx))

            text_buf = ""
            tool_calls: list = []
            stream = _active_client(ctx, ctx.active_backend).stream_complete(
                all_messages, tools or None, model=request_model, effort=request_effort,
                retry_model=retry_model, fallback_model=fallback_model,
            )
            if ctx.local_mode or ctx.active_backend == "local":
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
                    # HARD backend failure (402 billing / auth / missing model) — NEVER silent. Announce
                    # ONCE, mark the backend degraded so the ladder skips it (the floor moves up), and
                    # recover the turn on the next working rung. Only before any speech (else we'd replay).
                    bk = ctx.active_backend
                    if (not text_buf and bk and bk not in ctx.degraded_backends
                            and _is_hard_backend_error(e)):
                        ctx.degraded_backends.add(bk)
                        nxt = _reassemble_floor(ctx)
                        dlog(ctx, "backend_degraded", backend=bk,
                             cause=f"{type(e).__name__}: {e}"[:120],
                             recovered_to=(nxt[2] if nxt else None))
                        if nxt:
                            await emit(events.token(_degraded_notice(bk, nxt[2])))
                            ctx.active_model, ctx.active_effort, ctx.active_backend = nxt
                            ctx.voice_announced_model = nxt[0]
                            continue
                        # nothing left to fall to → surface the error below
                    # An escalated turn that fails to generate (gab.ai 'inference_failed') falls back to
                    # the simple model at the TURN level — a guaranteed arya answer beats a spoken error.
                    # Only safe before any speech was emitted (else we'd replay it), and only once.
                    if (not text_buf and not fell_back and cur != simple
                            and _is_transient_generation_error(e)):
                        fell_back = True
                        ctx.active_model = simple
                        ctx.active_effort = simple_effort
                        ctx.active_backend = simple_backend   # re-route to the floor's client
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
                if _is_media_command(ctx, cid):
                    media_ran = True
                ctx.session.append_message(
                    ChatMessage(role="tool", content=result.to_content(), tool_call_id=tc.id)
                )

            # Loop guard: count this round's signatures; a repeated one means the model is stuck firing
            # the same call with no progress. Step ONE rung up (sequential, least-escalation-first) to let
            # a more tool-capable model break the loop — but only on a command-intent turn (the class that
            # loops on tool-protocol fumbles) and only when an assembled ladder has a rung left. At the
            # ceiling, off the ladder, or past the hard round cap: stop and say so rather than hammer on.
            tool_rounds += 1
            stuck = None
            for tc, result in zip(tool_calls, results):
                s = _tool_sig(tc, result)
                sig_counts[s] = sig_counts.get(s, 0) + 1
                if sig_counts[s] >= _LOOP_REPEAT:
                    stuck = s
            if stuck or tool_rounds >= _MAX_TOOL_ROUNDS:
                nxt = None
                # A runtime PIN means "stay on this rung" — bail honestly rather than override it.
                if stuck and command_intent and router and assembled and not ctx.force_model:
                    nxt = router.next_rung(ctx.active_model, ctx.active_effort, ctx.active_backend)
                if nxt is not None:
                    sig_counts[stuck] = 0   # give the higher rung a clean shot before climbing again
                    ctx.active_model, ctx.active_effort, ctx.active_backend = (
                        nxt.model, nxt.effort or None, nxt.backend)
                    dlog(ctx, "loop_escalate", sig=stuck, to=nxt.model,
                         effort=nxt.effort or None, backend=nxt.backend, rounds=tool_rounds)
                    continue
                dlog(ctx, "loop_abort", sig=stuck, rounds=tool_rounds, model=ctx.active_model)
                if not text_buf:   # nothing spoken yet → give an honest line instead of silence
                    await emit(events.token(_LOOP_GIVEUP_MSG))
                    ctx.session.append_message(
                        ChatMessage(role="assistant", content=_LOOP_GIVEUP_MSG))
                    text_buf = _LOOP_GIVEUP_MSG
                break

        # Media-control keepalive: hold the wake/command window open for a follow-up command so the
        # wake-gate doesn't idle-close and lock the user out mid-interaction while music plays (refreshed
        # per media turn; the voice side releases on TTL expiry + its own max-hold ceiling). 0 disables.
        keepalive_secs = int(getattr(ctx.config, "media_keepalive_secs", 0) or 0)
        if media_ran and keepalive_secs > 0:
            await emit(events.keepalive(keepalive_secs))
            dlog(ctx, "keepalive", ttl_secs=keepalive_secs)

        # Conversation-hold release: tell the voice side to drop the bed-duck early when THIS turn is a
        # terminal one-shot reply, instead of holding it the full conversation-hold window after an
        # addressed reply over playing media. NOT on a media-control turn (more commands likely — the
        # keepalive above already holds the window) and NOT when the reply ends in a question (a
        # clarification/option list awaiting an answer). Arrival-keyed + safe to omit (degrades to the
        # voice-side timer), so emit conservatively. text_buf holds the final reply (loop broke on no tools).
        if (getattr(ctx.config, "voice_convo_hold_release", True)
                and not media_ran and _is_terminal_reply(text_buf)):
            await emit(events.convo_hold())
            dlog(ctx, "convo_hold", release=True)

        await emit(events.done())
    except asyncio.CancelledError:
        # A barge-in (/cancel) aborts the turn. Log it EXPLICITLY: a cancel bypasses the `except Exception`
        # error path and only `finally` logs `turn_done`, so without this a cancelled turn reads as a phantom
        # "silent drop" (no addressed/reply/error) — which once cost a whole debugging round chasing a
        # non-existent brain bug for what was really a spurious empty barge-in on the voice side.
        dlog(ctx, "cancelled", stage="turn", dur_ms=int((time.monotonic() - _t0) * 1000))
        if vs.queue is not None:
            vs.queue.put_nowait(events.done())
        raise
    except Exception as e:
        # Last-ditch G6 fallback (the reliable one — the in-loop guard wasn't catching the
        # escalate-after-tool path live). An escalated turn that died on a transient gab.ai
        # 'inference_failed' gets ONE plain arya narration of the current state — no tools, no
        # re-execution (the file/tool already ran) — instead of speaking "[gab.ai error]".
        # Resolve the floor independently (assembled → ladder[0]; legacy → provider simple) so the
        # narration runs on the floor's OWN client — in cross-backend mode ctx.client may be a
        # different backend than the floor model.
        from gabagent.agent.router import ModelRouter as _MR
        _fr = _MR.assemble(ctx.config, local_floor=ctx.local_floor,
                           local_running=ctx.local_client is not None,
                           degraded=ctx.degraded_backends)
        if _fr:
            _f0 = _fr.ladder[0]
            _simple, _simple_backend = _f0.model, _f0.backend
        else:
            _simple = (ctx.config.claude.ladder[0].model
                       if ctx.config.provider == "claude" else ctx.config.router.simple_model)
            _simple_backend = "claude" if ctx.config.provider == "claude" else "gab"
        if (not ctx.local_mode and vs.queue is not None
                and (ctx.active_model or _simple) != _simple
                and _is_transient_generation_error(e)):
            try:
                ctx.active_model = _simple
                ctx.active_backend = _simple_backend
                ctx.voice_announced_model = _simple
                msgs = _build_voice_messages(ctx, ctx.session.messages())
                text = await _active_client(ctx, _simple_backend).complete_simple(msgs, model=_simple)
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
        hard = _is_hard_backend_error(e)
        # Persist a full traceback for diagnosis (mirrors the TUI crash path in cli.py) — but NOT for
        # expected-and-handled backend states (402/auth/missing-model, or a network timeout). Those are
        # already classified and spoken gracefully below, so a full crash postmortem is pure noise (a
        # depleted-credits evening once wrote 5 tracebacks for what's really a billing/network state).
        if not (hard or _is_network_timeout(e)):
            try:
                import traceback
                from gabagent.session.postmortem import PostMortemManager
                PostMortemManager(cwd=ctx.cwd).log_crash(
                    f"Voice turn failed for: {user_text!r}\n\n{traceback.format_exc()}"
                )
            except Exception:
                pass
        dlog(ctx, "error", cause=cause, stage="turn", hard=hard)
        if vs is not None:
            vs.set_error(cause)
        if vs.queue is not None:
            # voice-agent speaks `error.text` and logs `summary`; the trailing `done` closes the SSE.
            # A HARD backend failure (billing/auth/missing-model) with no rung left to fall to won't fix
            # itself on retry — speak an HONEST line so the user doesn't keep re-asking against a dead
            # backend ("Did you hear me?… At all."). Everything else is a transient hiccup where "say
            # that again" is the right nudge.
            text = ("I can't reach my service right now, and retrying won't fix it — it looks like an "
                    "account or billing issue." if hard
                    else "Sorry, I had a brief hiccup — could you say that again?")
            vs.queue.put_nowait(events.error(cause, text))
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
