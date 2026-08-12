from __future__ import annotations
import asyncio
import sys
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class SpawnAgentTool(ToolBase):
    name = "spawn_agent"
    description = (
        "Spawn a sub-agent to handle a task with an isolated context window. "
        "Foreground mode: shows output inline. "
        "Background mode: runs independently, returns session ID."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Task for the sub-agent"},
            "mode": {
                "type": "string",
                "enum": ["foreground", "background"],
                "description": "foreground = inline nested agent, background = separate process",
            },
            "model": {"type": "string", "description": "Model override for sub-agent"},
        },
        "required": ["prompt"],
    }

    async def execute(
        self,
        ctx: AgentContext,
        prompt: str,
        mode: str = "foreground",
        model: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if mode == "background":
            return await _spawn_background(ctx, prompt, model)
        else:
            return await _spawn_foreground(ctx, prompt, model)


async def _spawn_foreground(ctx: AgentContext, prompt: str, model: str | None) -> ToolResult:
    from gabagent.config.loader import load_config
    from gabagent.api.factory import build_client
    from gabagent.api.rate_limit import RateLimiter
    from gabagent.agent.context import AgentContext as Ctx
    from gabagent.agent.system_prompt import build_system_prompt
    from gabagent.session.manager import SessionManager
    from gabagent.session.memory import MemoryManager
    from gabagent.agent.loop import run_loop
    from gabagent.tui.renderer import console

    cfg = ctx.config
    if model:
        import copy
        cfg = copy.copy(cfg)
        cfg.model = model

    mgr = SessionManager(cwd=ctx.cwd)
    sid, sf = mgr.create_session()
    mem_mgr = MemoryManager(ctx.cwd)
    memory = mem_mgr.load()
    sp = build_system_prompt(ctx.cwd, memory or None)

    sub_ctx = Ctx(
        config=cfg,
        client=build_client(cfg, ctx.rate_limiter, model=cfg.model if model else None),
        rate_limiter=ctx.rate_limiter,
        session=sf,
        session_id=sid,
        cwd=ctx.cwd,
        system_prompt=sp,
        headless=True,
        is_subagent=True,  # gate Stratum OFF: a sub-agent must not rewrite Current Focus or accrete habits
    )

    console.rule(f"[dim]Sub-agent {sid[:8]}[/dim]")
    try:
        await run_loop(sub_ctx, initial_prompt=prompt)
    except Exception as e:
        console.print(f"[error]Sub-agent error: {e}[/error]", markup=True)
    console.rule(f"[dim]Sub-agent done[/dim]")

    final_msgs = sf.messages()
    final_text = next(
        (m.content for m in reversed(final_msgs) if m.role == "assistant" and m.content),
        "(no output)",
    )
    return ToolResult(output=final_text)


async def _spawn_background(ctx: AgentContext, prompt: str, model: str | None) -> ToolResult:
    cmd = [sys.executable, "-m", "gabagent", "--headless", "--prompt", prompt, "--cwd", str(ctx.cwd)]
    if model:
        cmd += ["--model", model]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return ToolResult(output=f"Background sub-agent started (pid={proc.pid}). Check session logs for output.")
