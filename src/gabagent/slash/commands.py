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
        "/voice": _voice,
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
        ("/model [name]", "Show or switch model"),
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
        ("/local [on|off]", "Toggle local Ollama model (starts on demand)"),
        ("/voice [on|off]", "Start/stop the voice brain server (talk to it via voice-agent)"),
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
    if arg:
        ctx.config.model = arg
        ctx.client.model = arg
        console.print(f"[info]Model switched to: {arg}[/info]", markup=True)
    else:
        active = ctx.active_model or ctx.config.model
        console.print(f"[info]Current model: {active}[/info]", markup=True)
        console.print(f"[info]Usage: {ctx.rate_limiter.badge}[/info]", markup=True)


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

    sub = arg.strip().lower() or ("off" if ctx.local_mode else "on")

    if sub == "on" and not ctx.local_mode:
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
    from gabagent.voice.launcher import start_brain, stop_brain, brain_health

    parts = arg.strip().split()
    sub = parts[0].lower() if parts else "status"
    port = ctx.config.voice_port
    if len(parts) > 1 and parts[1].isdigit():
        port = int(parts[1])
    base = f"http://127.0.0.1:{port}"

    if sub in ("status", ""):
        up = await brain_health(base)
        if up:
            owner = "started here" if ctx.voice_process is not None else "external"
            console.print(
                f"[gab.accent]◆[/gab.accent] [dim]Voice brain: [green]ON[/green] — {base} ({owner})[/dim]",
                markup=True,
            )
        else:
            console.print("[dim]Voice brain: OFF. Use /voice on to start it.[/dim]", markup=True)

    elif sub == "on":
        console.print("[dim]Starting voice brain…[/dim]", markup=True)
        running, spawned, msg = await start_brain(ctx, port)
        if running:
            tag = "started" if spawned else "already running — attached"
            console.print(
                f"[gab.accent]◆[/gab.accent] [dim]Voice brain {tag} on {base}[/dim]", markup=True
            )
            console.print(
                "[dim]  Start voice-agent to talk. "
                "Tip: avoid typing here while talking — you share this conversation.[/dim]",
                markup=True,
            )
        else:
            from gabagent.config.paths import data_dir
            console.print(f"[error]Could not start voice brain: {msg}[/error]", markup=True)
            console.print(f"[dim]  See {data_dir() / 'voice-serve.log'}[/dim]", markup=True)

    elif sub == "off":
        if ctx.voice_process is not None:
            stop_brain(ctx)
            console.print("[dim]Voice brain stopped.[/dim]", markup=True)
        elif await brain_health(base):
            console.print(
                "[warning]A voice brain is running but wasn't started here — leaving it running. "
                "Stop it where it was launched.[/warning]",
                markup=True,
            )
        else:
            console.print("[dim]No voice brain to stop.[/dim]", markup=True)

    else:
        console.print("[warning]Usage: /voice [on|off|status][/warning]", markup=True)


async def _exit(arg: str, ctx: AgentContext) -> None:
    raise SystemExit(0)
