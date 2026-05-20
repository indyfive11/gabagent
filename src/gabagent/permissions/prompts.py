from __future__ import annotations
import json
from typing import Any, TYPE_CHECKING
from rich.panel import Panel
from rich.text import Text
from gabagent.tui.renderer import console

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


async def interactive_approve(tool_name: str, args: dict, ctx: AgentContext) -> bool:
    try:
        args_display = json.dumps(args, indent=2)[:500]
    except Exception:
        args_display = str(args)[:500]

    console.print(
        Panel(
            f"[bold]{tool_name}[/bold]\n[dim]{args_display}[/dim]",
            title="[yellow]Permission Required[/yellow]",
            border_style="yellow",
            width=min(72, console.width - 2),
        )
    )

    from prompt_toolkit import prompt as pt_prompt
    try:
        answer = await _async_prompt(
            "[y]es / [n]o / [a]lways allow / [N]ever allow: "
        )
    except (EOFError, KeyboardInterrupt):
        return False

    answer = answer.strip().lower()

    if answer in ("a", "always"):
        ctx.config.permissions.allow.append(f"{tool_name}(*)")
        from gabagent.permissions.engine import PermissionEngine
        ctx.config.permissions.allow.append(tool_name)
        return True
    elif answer in ("n", "no", ""):
        return False
    elif answer in ("never", "N"[0:1].lower()):
        ctx.config.permissions.deny.append(tool_name)
        return False
    elif answer in ("y", "yes"):
        return True

    return False


async def _async_prompt(message: str) -> str:
    from prompt_toolkit import PromptSession
    session = PromptSession()
    return await session.prompt_async(message)
