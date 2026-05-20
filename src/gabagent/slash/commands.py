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


async def _exit(arg: str, ctx: AgentContext) -> None:
    raise SystemExit(0)
