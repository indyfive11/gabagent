from __future__ import annotations
import json
from typing import TYPE_CHECKING
from gabagent.tui.renderer import console

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


async def handle_slash(command: str, ctx: AgentContext) -> bool:
    parts = command.strip().split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    handlers = {
        "/help": _help,
        "/clear": _clear,
        "/compact": _compact,
        "/model": _model,
        "/cost": _cost,
        "/usage": _usage,
        "/memory": _memory,
        "/tools": _tools,
        "/rename": _rename,
        "/plan": _plan,
        "/approve": _approve,
        "/fork": _fork,
        "/resume": _resume,
        "/config": _config,
        "/msg": _msg,
        "/inbox": _inbox,
        "/local": _local,
        "/backend": _backend,
        "/persona": _persona,
        "/voice": _voice,
        "/skills": _skills,
        "/attestation": _attestation,
        "/exit": _exit,
        "/quit": _exit,
    }

    handler = handlers.get(cmd)
    if handler:
        await handler(arg, ctx)
        return True
    console.print(f"[warning]Unknown command: {cmd}. Try /help[/warning]", markup=True)
    return True


async def _help(arg: str, ctx: AgentContext) -> None:
    from rich.table import Table
    t = Table(title="Gab-Agent Commands", show_header=True)
    t.add_column("Command", style="cyan")
    t.add_column("Description")
    rows = [
        ("/help", "Show this help"),
        ("/clear", "Clear screen and reset display"),
        ("/compact", "Compress conversation context"),
        ("/model [name|auto]", "Pin one model (opus/sonnet/haiku/aria/local) — no escalation; /model auto to resume"),
        ("/cost", "Show token usage and model info"),
        ("/usage", "Detailed session usage"),
        ("/memory", "Show persistent memory"),
        ("/tools", "Show list of available capabilities"),
        ("/rename <name>", "Rename the current conversation"),
        ("/plan", "Enter/exit plan mode manually (write_plan auto-enters)"),
        ("/approve", "Approve the current plan and allow execution to proceed"),
        ("/fork", "Fork current session"),
        ("/resume", "List and resume a past session"),
        ("/config", "Show current configuration"),
        ("/msg <text>", "Send a message to Claude Code"),
        ("/inbox", "Check messages from Claude Code"),
        ("/local [on|off|only]", "on=local as the floor rung (escalates); only=exclusive; off=back to Aria"),
        ("/backend [gab|claude]", "Show or switch the LLM backend (the brain)"),
        ("/persona [show|reset|on|off]", "Aria's learned personality — show it, reset to seed, or toggle"),
        ("/voice [on|off|brain]", "on/start=brain+mic (talk by voice); off/stop=stop; bare /voice toggles"),
        ("/skills [list|install <path>]", "Manage attested skill plugins (capabilities)"),
        ("/attestation", "View/set how skill plugins are vetted"),
        ("/exit", "Exit Gab-Agent"),
    ]
    for cmd, desc in rows:
        t.add_row(cmd, desc)
    console.print(t)


async def _clear(arg: str, ctx: AgentContext) -> None:
    import os
    os.system("clear")


async def _compact(arg: str, ctx: AgentContext) -> None:
    from gabagent.agent.loop import _compact_context
    await _compact_context(ctx)


async def _model(arg: str, ctx: AgentContext) -> None:
    a = arg.strip().lower()
    if not a:
        if ctx.force_model and ctx.pinned_backend:
            eff = f"/{ctx.pinned_effort}" if ctx.pinned_effort else ""
            console.print(
                f"[info]Pinned to {ctx.pinned_model}{eff} ({ctx.pinned_backend}). "
                "Escalation OFF — /model auto to resume.[/info]", markup=True)
        else:
            console.print(
                f"[info]Auto-escalation ON (Aria → Claude). Pin one model with /model <name>.[/info]",
                markup=True)
        console.print("[dim]names: opus · sonnet · haiku · aria · local  (add effort: 'opus max')[/dim]",
                      markup=True)
        return
    if a in ("auto", "off", "ladder", "escalate", "automatic"):
        ctx.force_model = False
        ctx.pinned_model = ctx.pinned_effort = ctx.pinned_backend = None
        ctx.active_model = ctx.active_effort = ctx.active_backend = None
        console.print("[info]Auto-escalation resumed (Aria → Claude).[/info]", markup=True)
        return
    if a in ("list", "?", "names"):
        console.print("[info]opus · sonnet · haiku (Claude) · aria (Gab) · local/devstral (Ollama). "
                      "Add an effort for Claude: 'opus max'.[/info]", markup=True)
        return
    from gabagent.agent.router import resolve_pin
    res = resolve_pin(a, ctx.config)
    if res is None:
        console.print(f"[warning]Unknown model '{arg}'. Try /model list[/warning]", markup=True)
        return
    model, effort, backend = res
    if backend == "local":
        from gabagent.local.ollama import ensure_ollama_running, _build_local_client, _FLOOR_KEEP_ALIVE
        console.print("[dim]Starting local model…[/dim]", markup=True)
        err = await ensure_ollama_running(ctx)
        if err:
            console.print(f"[error]Can't start local: {err}[/error]", markup=True)
            return
        if ctx.local_client is None:
            ctx.local_client = _build_local_client(ctx, _FLOOR_KEEP_ALIVE)
    ctx.force_model = True
    ctx.pinned_model, ctx.pinned_effort, ctx.pinned_backend = model, effort, backend
    eff = f"/{effort}" if effort else ""
    console.print(f"[info]Pinned to {model}{eff}. Escalation OFF — /model auto to resume.[/info]",
                  markup=True)


async def _backend(arg: str, ctx: AgentContext) -> None:
    target = arg.strip().lower()
    if not target:
        prov = ctx.config.provider
        base = ctx.config.claude.ladder[0].model if prov == "claude" else ctx.config.model
        console.print(f"[info]Backend: {prov}  (base model: {base})[/info]", markup=True)
        console.print("[dim]Switch with: /backend gab  ·  /backend claude[/dim]", markup=True)
        return
    if target not in ("gab", "claude"):
        console.print(f"[warning]Unknown backend '{target}'. Use: /backend gab|claude[/warning]", markup=True)
        return

    import os
    if target == "claude" and not (ctx.config.claude.api_key or os.environ.get("ANTHROPIC_API_KEY")):
        console.print(
            "[warning]No Anthropic key set. Run 'gab --set-claude-key <key>' or set ANTHROPIC_API_KEY first.[/warning]",
            markup=True,
        )
        return

    # 'anthropic' is an optional package (absent on a default AUR install). Refuse the switch with an
    # install hint BEFORE mutating provider, so a missing package can't leave provider half-switched.
    if target == "claude":
        from gabagent.api.factory import anthropic_available
        if not anthropic_available():
            console.print(
                "[warning]The Claude backend needs the 'anthropic' package, which isn't installed. "
                "Install it:  pacman -S python-anthropic   (or:  pip install anthropic)[/warning]",
                markup=True,
            )
            return

    ctx.config.provider = target
    if target == "claude":
        ctx.config.model = ctx.config.claude.ladder[0].model
    else:  # gab — reset the base model to Aria so a stale Claude id doesn't linger in the badge/client
        ctx.config.model = ctx.config.router.simple_model
    # Reset per-turn routing state so the next turn re-classifies on the new provider.
    ctx.active_model = None
    ctx.active_effort = None

    from gabagent.api.factory import build_client
    ctx.client = build_client(ctx.config, ctx.rate_limiter)

    from gabagent.config.loader import save_config
    save_config(ctx.config)
    console.print(f"[info]Backend switched to: {target}  (base model: {ctx.client.model})[/info]", markup=True)


async def _persona(arg: str, ctx: AgentContext) -> None:
    from gabagent.persona.manager import PersonaManager
    from gabagent.session.memory import _rebuild_system_prompt
    mgr = PersonaManager()
    sub = arg.strip().lower() or "show"

    if sub == "show":
        if not ctx.config.persona_enabled:
            console.print("[dim]Persona learning is OFF. /persona on to enable.[/dim]", markup=True)
        from rich.markdown import Markdown
        seed, idx = mgr.seed(), mgr.index()
        console.print("[info]— Seed (the floor) —[/info]", markup=True)
        console.print(Markdown(seed or "(none)"))
        console.print("[info]— Learned with this user —[/info]", markup=True)
        console.print(Markdown(idx or "_(nothing learned yet — talk to her and it grows at shutdown)_"))
        hint = mgr.voice_hint()
        if hint.get("character"):
            console.print(
                f"[dim]Suggested TTS voice character: {hint['character']} "
                f"(confidence {hint.get('confidence', 0)}) — voice side confirms/overrides[/dim]",
                markup=True,
            )
        return

    if sub == "reset":
        mgr.reset()
        ctx.system_prompt = _rebuild_system_prompt(ctx)
        console.print("[info]Persona reset to seed (learned traits + journal cleared).[/info]", markup=True)
        return

    if sub in ("on", "off"):
        ctx.config.persona_enabled = sub == "on"
        from gabagent.config.loader import save_config
        save_config(ctx.config)
        ctx.system_prompt = _rebuild_system_prompt(ctx)
        console.print(f"[info]Persona learning {'ON' if sub == 'on' else 'OFF'}.[/info]", markup=True)
        return

    console.print("[warning]Usage: /persona [show|reset|on|off][/warning]", markup=True)


async def _cost(arg: str, ctx: AgentContext) -> None:
    console.print(f"[info]{ctx.rate_limiter.badge}[/info]", markup=True)
    console.print(f"[info]Estimated tokens this context: ~{ctx.token_estimate:,}[/info]", markup=True)
    console.print(f"[info]Context limit: {ctx.config.max_context_tokens:,}[/info]", markup=True)


async def _usage(arg: str, ctx: AgentContext) -> None:
    await _cost(arg, ctx)
    msgs = ctx.session.messages()
    user_turns = sum(1 for m in msgs if m.role == "user")
    assistant_turns = sum(1 for m in msgs if m.role == "assistant")
    tool_turns = sum(1 for m in msgs if m.role == "tool")
    console.print(
        f"[info]Session {ctx.session_id[:8]}: "
        f"{user_turns} user / {assistant_turns} assistant / {tool_turns} tool messages[/info]",
        markup=True,
    )


async def _memory(arg: str, ctx: AgentContext) -> None:
    from gabagent.session.memory import MemoryManager
    mgr = MemoryManager(ctx.cwd)
    content = mgr.load()
    if not content:
        console.print("[info]No memory saved for this project.[/info]", markup=True)
    else:
        from rich.markdown import Markdown
        console.print(Markdown(content))
    if arg == "clear":
        mgr.clear()
        console.print("[info]Memory cleared.[/info]", markup=True)


async def _tools(arg: str, ctx: AgentContext) -> None:
    from gabagent.tools.registry import registry
    from rich.table import Table
    schemas = registry.get_schemas()
    t = Table(title="Available Tools", show_header=True)
    t.add_column("Tool", style="cyan", no_wrap=True)
    t.add_column("Description")
    for schema in sorted(schemas, key=lambda s: s.get("function", {}).get("name", "")):
        func = schema.get("function", {})
        name = func.get("name", "unknown")
        desc = func.get("description", "")
        t.add_row(name, desc[:80] + ("..." if len(desc) > 80 else ""))
    console.print(t)


async def _rename(arg: str, ctx: AgentContext) -> None:
    if not arg.strip():
        console.print("[warning]/rename requires a name: /rename <name>[/warning]", markup=True)
        return
    from gabagent.session.manager import SessionManager
    mgr = SessionManager(ctx.cwd)
    mgr.set_session_name(ctx.session_id, arg.strip())
    console.print(f"[info]Session renamed to: {arg.strip()}[/info]", markup=True)


async def _plan(arg: str, ctx: AgentContext) -> None:
    from gabagent.plan.mode import enter_plan_mode, exit_plan_mode
    if ctx.plan_mode:
        exit_plan_mode(ctx)
        console.print("[info]Plan mode disabled. Normal operation resumed.[/info]", markup=True)
    else:
        enter_plan_mode(ctx)
        console.print(
            "[info]Plan mode enabled. Writes and shell mutations are blocked. "
            "Use /plan again to exit.[/info]",
            markup=True,
        )


async def _approve(arg: str, ctx: AgentContext) -> None:
    from gabagent.plan.mode import exit_plan_mode
    if not ctx.plan_mode:
        console.print("[dim]No active plan — not in plan mode.[/dim]", markup=True)
        return
    exit_plan_mode(ctx)
    console.print(
        "[gab.accent]◆[/gab.accent] [dim]Plan approved. Proceeding with implementation.[/dim]",
        markup=True,
    )
    from gabagent.api.models import ChatMessage
    ctx.session.append_message(ChatMessage(role="user", content="Plan approved. Proceed with the implementation."))


async def _fork(arg: str, ctx: AgentContext) -> None:
    from gabagent.session.manager import SessionManager
    mgr = SessionManager(ctx.cwd)
    new_id, _ = mgr.fork_session(ctx.session_id)
    console.print(
        f"[info]Forked: new session {new_id}. "
        f"Start `gabagent --resume {new_id}` to use it.[/info]",
        markup=True,
    )


async def _resume(arg: str, ctx: AgentContext) -> None:
    from gabagent.session.manager import SessionManager
    mgr = SessionManager(ctx.cwd)
    sessions = mgr.list_sessions()
    if not sessions:
        console.print("[info]No sessions found.[/info]", markup=True)
        return
    from rich.table import Table
    import datetime
    t = Table(title="Recent Sessions")
    t.add_column("#", style="dim")
    t.add_column("ID", style="cyan")
    t.add_column("Date")
    t.add_column("Messages")
    t.add_column("Preview")
    for i, s in enumerate(sessions[:10]):
        dt = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        t.add_row(str(i + 1), s["id"][:8], dt, str(s["message_count"]), s["preview"])
    console.print(t)
    console.print("[dim]Run `gabagent --resume <full-uuid>` to resume.[/dim]", markup=True)


async def _config(arg: str, ctx: AgentContext) -> None:
    from rich.pretty import Pretty
    data = ctx.config.model_dump()
    data.pop("api_key", None)
    console.print(Pretty(data))


async def _msg(arg: str, ctx: AgentContext) -> None:
    """Send a message to Claude Code via the bridge."""
    if not arg:
        console.print("[warning]/msg requires a message: /msg <text>[/warning]", markup=True)
        return
    try:
        from gabagent.bridge import gab_to_claude
        gab_to_claude(arg, topic="gab")
        console.print("[info]Message queued for Claude Code.[/info]", markup=True)
    except Exception as e:
        console.print(f"[error]Bridge error: {e}[/error]", markup=True)


async def _inbox(arg: str, ctx: AgentContext) -> None:
    """Check for messages from Claude Code."""
    try:
        from gabagent.bridge import read_inbox
        messages = read_inbox(mark_read=True)
        if not messages:
            console.print("[dim]No messages from Claude Code.[/dim]", markup=True)
            return
        console.print("[dim]── messages from Claude Code ──[/dim]", markup=True)
        for msg in messages:
            topic = msg.get("topic", "note")
            console.print(
                f"[bold cyan][{topic}][/bold cyan] {msg['message']}", markup=True
            )
        console.print("[dim]────────────────────────────────[/dim]", markup=True)
    except Exception as e:
        console.print(f"[error]Bridge error: {e}[/error]", markup=True)


async def _local(arg: str, ctx: AgentContext) -> None:
    if not ctx.config.local_model:
        console.print(
            "[warning]No local model configured. "
            "Set local_model in ~/.config/gabagent/settings.json[/warning]",
            markup=True,
        )
        return

    sub = arg.strip().lower() or ("off" if (ctx.local_mode or ctx.local_floor) else "on")

    # "/local on" (and "floor") = make local the warm FLOOR rung (router still escalates Aria → Claude).
    # "/local only" = exclusive local (no escalation). "/local off" turns off whichever is active.
    if sub in ("on", "floor"):
        from gabagent.local.ollama import start_local_floor
        console.print("[dim]Warming local as the floor rung… (ROCm GPU init takes ~30s)[/dim]", markup=True)
        err = await start_local_floor(ctx)
        if err:
            console.print(f"[error]Could not start local floor: {err}[/error]", markup=True)
            return
        console.print(
            f"[gab.accent]◆[/gab.accent] [dim]Local floor ON — {ctx.config.local_model} → Aria → Claude[/dim]",
            markup=True,
        )
        return
    if sub in ("aria",) or (sub == "off" and ctx.local_floor and not ctx.local_mode):
        from gabagent.local.ollama import stop_local_floor
        await stop_local_floor(ctx)
        console.print(
            "[gab.accent]◆[/gab.accent] [dim]Floor → Aria (local unloaded, VRAM freed)[/dim]",
            markup=True,
        )
        return

    if sub in ("only", "exclusive") and not ctx.local_mode:
        from gabagent.local.ollama import ensure_ollama_running
        from gabagent.api.client import GabAIClient
        console.print("[dim]Starting local model… (ROCm GPU init takes ~30s)[/dim]", markup=True)
        err = await ensure_ollama_running(ctx)
        if err:
            console.print(f"[error]Could not start Ollama: {err}[/error]", markup=True)
            return
        if ctx.local_client is None:
            ctx.local_client = GabAIClient(
                api_key="ollama",
                base_url=ctx.config.local_base_url,
                model=ctx.config.local_model,
                rate_limiter=ctx.rate_limiter,
                keep_alive="1m",  # short idle unload; gabagent-scoped
            )
        ctx.local_mode = True

        # Generate a compact briefing of the session so far for the local model.
        # The local model only sees this summary + the last few messages, not the
        # full growing history — this is the key to keeping latency manageable.
        from gabagent.api.models import ChatMessage
        messages = ctx.session.messages()
        user_assistant = [m for m in messages if m.role in ("user", "assistant")]
        if user_assistant:
            console.print("[dim]Generating session briefing…[/dim]", markup=True)
            summary_prompt = [
                ChatMessage(role="system", content="You are a concise technical summarizer."),
                *user_assistant[-20:],
                ChatMessage(role="user", content=(
                    "Summarize this conversation into a compact briefing (200-350 words) for a "
                    "local coding assistant that will continue the work. Include: current task/goal, "
                    "key files and code locations, recent decisions and findings, pending work. "
                    "Be specific — include file paths and function names. Start with '## Session Briefing\\n\\n'."
                )),
            ]
            try:
                ctx.local_context_summary = await ctx.client.complete_simple(summary_prompt)
            except Exception:
                ctx.local_context_summary = None

        ctx.session.append_message(ChatMessage(
            role="system",
            content=(
                f"[Local model session started] "
                f"{ctx.config.local_model} is now the active assistant. "
                "The following assistant turns are generated by the local model, not by you."
            ),
        ))
        console.print(
            f"[gab.accent]◆[/gab.accent] [dim]Local mode ON — {ctx.config.local_model}[/dim]",
            markup=True,
        )

    elif sub == "off" and ctx.local_mode:
        ctx.local_mode = False
        from gabagent.local.ollama import unload_local
        await unload_local(ctx)  # free VRAM immediately on leaving local
        from gabagent.api.models import ChatMessage
        ctx.session.append_message(ChatMessage(
            role="system",
            content=(
                f"[Local model session ended] "
                f"The assistant turns since the previous 'Local model session started' marker "
                f"were generated by {ctx.config.local_model} (local model), not by you. "
                "You are now the active assistant again. "
                "The full local model conversation is in your context above."
            ),
        ))
        console.print("[dim]Local mode OFF — using primary model[/dim]", markup=True)

    else:
        state = "[green]ON[/green]" if ctx.local_mode else "[dim]OFF[/dim]"
        model = ctx.config.local_model or "(not configured)"
        console.print(f"[dim]Local mode: {state}  model: {model}[/dim]", markup=True)


async def _voice(arg: str, ctx: AgentContext) -> None:
    from gabagent.voice.launcher import (
        start_brain, stop_brain, brain_health, start_frontend, stop_frontend,
    )

    parts = arg.strip().split()
    sub = parts[0].lower() if parts else "toggle"
    # Intuitive aliases: start→on, stop→off, listen→on, mute→off.
    sub = {"start": "on", "listen": "on", "stop": "off", "mute": "off", "quit": "off"}.get(sub, sub)
    port = ctx.config.voice_port
    if len(parts) > 1 and parts[1].isdigit():
        port = int(parts[1])
    base = f"http://127.0.0.1:{port}"

    # Bare "/voice" toggles: stop if the brain is up, else start it.
    if sub == "toggle":
        sub = "off" if await brain_health(base) else "on"

    if sub in ("status", ""):
        up = await brain_health(base)
        if up:
            owner = "started here" if ctx.voice_process is not None else "external"
            fe = (ctx.voice_frontend_process is not None
                  and ctx.voice_frontend_process.poll() is None)
            console.print(
                f"[gab.accent]◆[/gab.accent] [dim]Voice brain: [green]ON[/green] — {base} ({owner})  "
                f"front-end: {'[green]listening[/green]' if fe else 'not started here'}[/dim]",
                markup=True,
            )
        else:
            console.print("[dim]Voice brain: OFF. Use /voice on to start it.[/dim]", markup=True)

    elif sub in ("on", "brain"):
        want_frontend = sub == "on"
        console.print("[dim]Starting voice brain…[/dim]", markup=True)
        running, spawned, msg = await start_brain(ctx, port)
        if not running:
            from gabagent.config.paths import data_dir
            console.print(f"[error]Could not start voice brain: {msg}[/error]", markup=True)
            console.print(f"[dim]  See {data_dir() / 'voice-serve.log'}[/dim]", markup=True)
            return

        tag = "started" if spawned else "already running — attached"
        console.print(
            f"[gab.accent]◆[/gab.accent] [dim]Voice brain {tag} on {base}[/dim]", markup=True
        )

        if not want_frontend:
            console.print(
                "[dim]  Brain only. Start voice-agent yourself to talk, or use /voice on next time.[/dim]",
                markup=True,
            )
            return

        # Only grab the mic when WE own the brain — an external brain almost certainly already has a
        # front-end attached, and a second one would double-capture the mic.
        if not spawned:
            console.print(
                "[dim]  Brain was already running — assuming a front-end is attached. "
                "Not starting another (use /voice brain to skip this).[/dim]",
                markup=True,
            )
            return

        console.print("[dim]Starting voice-agent front-end (so the brain can hear)…[/dim]", markup=True)
        fe_ok, fe_msg = await start_frontend(ctx, port)
        if fe_ok:
            console.print(
                "[gab.accent]◆[/gab.accent] [dim]Front-end listening — say your wake word to continue "
                "this conversation by voice.[/dim]",
                markup=True,
            )
            console.print(
                "[dim]  Tip: avoid typing here while talking — you share this conversation.[/dim]",
                markup=True,
            )
        else:
            from gabagent.config.paths import data_dir
            console.print(
                f"[warning]Brain is up, but the front-end didn't start: {fe_msg}[/warning]", markup=True
            )
            console.print(
                f"[dim]  Start voice-agent manually to talk. See {data_dir() / 'voice-frontend.log'}[/dim]",
                markup=True,
            )

    elif sub == "off":
        stopped_any = False
        if ctx.voice_frontend_process is not None:
            stop_frontend(ctx)
            console.print("[dim]Voice front-end stopped.[/dim]", markup=True)
            stopped_any = True
        if ctx.voice_process is not None:
            stop_brain(ctx)
            console.print("[dim]Voice brain stopped.[/dim]", markup=True)
            stopped_any = True
        if not stopped_any:
            if await brain_health(base):
                console.print(
                    "[warning]A voice brain is running but wasn't started here — leaving it running. "
                    "Stop it where it was launched.[/warning]",
                    markup=True,
                )
            else:
                console.print("[dim]No voice brain to stop.[/dim]", markup=True)

    else:
        console.print("[warning]Usage: /voice [on|brain|off|status][/warning]", markup=True)


_TIER_NAME = {1: "auto", 2: "spoken-yes", 3: "keyboard"}


async def _skills(arg: str, ctx: AgentContext) -> None:
    import json as _json
    from pathlib import Path
    from gabagent.commands.skills.loader import parse_manifest, SkillError, skills_root
    from gabagent.commands.skills.qualify import (
        qualify_skill, write_record, list_installed,
    )

    parts = arg.strip().split(None, 1)
    sub = parts[0].lower() if parts else "list"
    rest = parts[1].strip() if len(parts) > 1 else ""

    async def _rediscover() -> None:
        from gabagent.commands.discovery import discover_capabilities
        ctx.command_catalog = await discover_capabilities(ctx)

    def _set_enabled(skill_id: str, enabled: bool) -> bool:
        rf = skills_root() / skill_id / "attestation.json"
        if not rf.exists():
            return False
        rec = _json.loads(rf.read_text(encoding="utf-8"))
        rec["enabled"] = enabled
        rf.write_text(_json.dumps(rec, indent=2), encoding="utf-8")
        return True

    if sub in ("list", ""):
        installed = list_installed()
        if not installed:
            console.print("[dim]No skills installed. Use /skills install <path/to/skill.toml>[/dim]", markup=True)
            return
        from rich.table import Table
        t = Table(title="Installed skills", show_header=True)
        for col in ("Skill", "Enabled", "Risk", "Commands (tier)"):
            t.add_column(col)
        for r in installed:
            tiers = ", ".join(f"{cid}={_TIER_NAME.get(tt, tt)}" for cid, tt in (r.get("effective_tiers") or {}).items())
            t.add_row(
                r.get("skill_id", "?"),
                "[green]yes[/green]" if r.get("enabled") else "[dim]no[/dim]",
                "[red]dangerous[/red]" if r.get("dangerous") else "ok",
                tiers,
            )
        console.print(t)
        return

    if sub == "install":
        if rest.startswith(("http://", "https://")):
            try:
                import httpx
                raw = httpx.get(rest, timeout=15, follow_redirects=True).content
            except Exception as e:
                console.print(f"[error]Couldn't fetch {rest}: {e}[/error]", markup=True)
                return
        else:
            path = Path(rest).expanduser()
            if not rest or not path.exists():
                console.print(f"[error]No such skill file: {rest}[/error]", markup=True)
                return
            raw = path.read_bytes()
        try:
            manifest = parse_manifest(raw)
        except SkillError as e:
            console.print(f"[error]Rejected (malformed/disallowed): {e}[/error]", markup=True)
            return
        console.print(f"[dim]Qualifying '{manifest.id}' (attesting commands)…[/dim]", markup=True)
        qual = await qualify_skill(manifest, ctx)
        if qual.rejected:
            console.print(f"[error]REJECTED: {qual.reason}[/error]", markup=True)
            return
        _print_verdict(manifest, qual)
        approved = await _confirm_install(qual)
        write_record(manifest, qual, approved=approved, enabled=approved)
        if approved:
            await _rediscover()
            console.print(
                f"[gab.accent]◆[/gab.accent] [dim]Enabled '{manifest.id}' "
                f"({len(manifest.commands)} commands).[/dim]", markup=True,
            )
        else:
            console.print("[dim]Declined — installed but disabled.[/dim]", markup=True)
        return

    if sub in ("enable", "disable") and rest:
        if _set_enabled(rest, sub == "enable"):
            await _rediscover()
            console.print(f"[dim]{rest} {sub}d.[/dim]", markup=True)
        else:
            console.print(f"[warning]No such skill: {rest}[/warning]", markup=True)
        return

    if sub == "remove" and rest:
        import shutil
        d = skills_root() / rest
        if d.exists():
            shutil.rmtree(d)
            await _rediscover()
            console.print(f"[dim]Removed {rest}.[/dim]", markup=True)
        else:
            console.print(f"[warning]No such skill: {rest}[/warning]", markup=True)
        return

    if sub == "reattest" and rest:
        toml_f = skills_root() / rest / "skill.toml"
        if not toml_f.exists():
            console.print(f"[warning]No such skill: {rest}[/warning]", markup=True)
            return
        manifest = parse_manifest(toml_f.read_bytes())
        qual = await qualify_skill(manifest, ctx)
        if qual.rejected:
            _set_enabled(rest, False)
            console.print(f"[error]Now REJECTED: {qual.reason} — disabled.[/error]", markup=True)
            return
        _print_verdict(manifest, qual)
        approved = await _confirm_install(qual)
        write_record(manifest, qual, approved=approved, enabled=approved)
        await _rediscover()
        return

    console.print(
        "[warning]Usage: /skills [list | install <path> | enable <id> | disable <id> | "
        "remove <id> | reattest <id>][/warning]", markup=True,
    )


def _print_verdict(manifest, qual) -> None:
    from rich.panel import Panel
    lines = [f"[bold]{manifest.name}[/bold]  [dim]({manifest.id} v{manifest.version})[/dim]"]
    if manifest.description:
        lines.append(f"[dim]{manifest.description}[/dim]")
    lines.append("")
    for cid, tier in qual.effective.items():
        marker = "[red]" if tier >= 3 else ("[yellow]" if tier == 2 else "[green]")
        lines.append(f"  {marker}{cid} → {_TIER_NAME.get(tier, tier)} (tier {tier})[/]")
    if qual.flags:
        lines.append("")
        lines.append("[red]Flags:[/red] " + "; ".join(qual.flags))
    if qual.explanation:
        lines.append("")
        lines.append(f"[dim]Review: {qual.explanation}[/dim]")
    border = "red" if qual.dangerous else "yellow"
    title = "[red]⚠ DANGEROUS SKILL[/red]" if qual.dangerous else "[yellow]Skill review[/yellow]"
    console.print(Panel("\n".join(lines), title=title, border_style=border))


async def _confirm_install(qual) -> bool:
    if qual.dangerous:
        console.print(
            "[red]This skill contains actions that can change your system or are irreversible. "
            "You are the final arbiter — enabling it is your call.[/red]", markup=True,
        )
        try:
            ans = await _async_prompt("Type 'I accept the risk' to enable, anything else to decline: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return ans.strip().lower() == "i accept the risk"
    try:
        ans = await _async_prompt("Enable this skill? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() in ("y", "yes")


async def _async_prompt(message: str) -> str:
    from prompt_toolkit import PromptSession
    return await PromptSession().prompt_async(message)


async def _attestation(arg: str, ctx: AgentContext) -> None:
    cfg = ctx.config.attestation
    parts = arg.strip().split()
    if not parts:
        console.print(
            f"[dim]Attestation — reviewer=[cyan]{cfg.reviewer}[/cyan]  "
            f"model={cfg.model or '(complex model)'}  "
            f"reject_obfuscation={cfg.auto_reject_obfuscation}[/dim]", markup=True,
        )
        console.print(
            "[dim]  /attestation reviewer <claude_api|claude_code_bridge|off> · "
            "/attestation reject-obfuscation <on|off>[/dim]", markup=True,
        )
        return
    key = parts[0].lower()
    val = parts[1] if len(parts) > 1 else ""
    if key == "reviewer" and val in ("claude_api", "claude_code_bridge", "off"):
        cfg.reviewer = val
        console.print(f"[dim]reviewer = {val}[/dim]", markup=True)
    elif key in ("reject-obfuscation", "reject_obfuscation"):
        cfg.auto_reject_obfuscation = val.lower() in ("on", "true", "1", "yes")
        console.print(f"[dim]reject_obfuscation = {cfg.auto_reject_obfuscation}[/dim]", markup=True)
    else:
        console.print(
            "[warning]Usage: /attestation [reviewer <claude_api|claude_code_bridge|off>] "
            "[reject-obfuscation <on|off>][/warning]", markup=True,
        )


async def _exit(arg: str, ctx: AgentContext) -> None:
    raise SystemExit(0)
