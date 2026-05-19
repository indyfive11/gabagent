from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from typing import Optional
import typer

app = typer.Typer(name="gab", help="Gab-Agent: AI coding assistant powered by Gab AI")


def _build_context(
    api_key: str,
    model: str,
    session_id: str | None,
    continue_: bool,
    fork: str | None,
    cwd: Path,
    headless: bool,
) -> tuple:
    from gabagent.config.loader import load_config
    from gabagent.config.models import GabAgentConfig
    from gabagent.api.client import GabAIClient
    from gabagent.api.rate_limit import UsageTracker
    from gabagent.agent.context import AgentContext
    from gabagent.agent.system_prompt import build_system_prompt
    from gabagent.session.manager import SessionManager
    from gabagent.session.memory import MemoryManager

    overrides = {}
    if api_key:
        overrides["api_key"] = api_key
    if model:
        overrides["model"] = model

    cfg = load_config(overrides)

    rate_limiter = UsageTracker(simple_model=cfg.router.simple_model)
    client = GabAIClient(
        api_key=cfg.api_key or "__setup_pending__",
        base_url=cfg.base_url,
        model=cfg.model,
        rate_limiter=rate_limiter,
    )

    mgr = SessionManager(cwd=cwd)

    if fork:
        sid, sf = mgr.fork_session(fork)
        typer.echo(f"Forked session {fork} → {sid}")
    elif session_id:
        sid, sf = mgr.resume_session(session_id)
    elif continue_:
        result = mgr.continue_last()
        if result:
            sid, sf = result
            typer.echo(f"Continuing session {sid}")
        else:
            sid, sf = mgr.create_session()
    else:
        sid, sf = mgr.create_session()

    mem_mgr = MemoryManager(cwd=cwd)
    memory = mem_mgr.load()

    system_prompt = build_system_prompt(cwd=cwd, memory=memory or None)

    ctx = AgentContext(
        config=cfg,
        client=client,
        rate_limiter=rate_limiter,
        session=sf,
        session_id=sid,
        cwd=cwd,
        system_prompt=system_prompt,
        headless=headless,
    )
    return ctx


@app.command()
def main(
    prompt: Optional[str] = typer.Argument(None, help="One-shot prompt (non-interactive)"),
    api_key: str = typer.Option("", "--api-key", "-k", help="Gab AI API key"),
    model: str = typer.Option("", "--model", "-m", help="Model name (default: arya)"),
    continue_: bool = typer.Option(False, "--continue", "-c", help="Continue last session"),
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume session by UUID"),
    fork: Optional[str] = typer.Option(None, "--fork", help="Fork session by UUID"),
    headless: bool = typer.Option(False, "--headless", hidden=True, help="No TUI (for sub-agents)"),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", hidden=True, help="Working directory"),
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
) -> None:
    if version:
        from gabagent import __version__
        typer.echo(f"gab-agent {__version__}")
        raise typer.Exit()

    from gabagent.tui.renderer import console

    # Import all tool modules to register them
    _register_tools()

    try:
        ctx = _build_context(
            api_key=api_key,
            model=model,
            session_id=resume,
            continue_=continue_,
            fork=fork,
            cwd=cwd,
            headless=headless,
        )
    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Startup error: {e}", err=True)
        raise typer.Exit(1)

    # Force model flag: skip routing when --model was explicitly passed
    ctx.force_model = bool(model)

    if not headless:
        from gabagent import __version__
        badge = (
            ctx.rate_limiter.forced_badge(ctx.config.model)
            if ctx.force_model
            else ctx.rate_limiter.badge
        )
        console.print(
            f"[bold]Gab-Agent[/bold] [dim]v{__version__}[/dim]  "
            f"session=[dim]{ctx.session_id[:8]}[/dim]  "
            f"[dim]{badge}[/dim]",
            markup=True,
        )
        console.print("[dim]Type /help for commands. Ctrl-D or /exit to quit.[/dim]", markup=True)

    asyncio.run(_run(ctx, prompt))


async def _run(ctx, prompt: str | None) -> None:
    from gabagent.agent.loop import run_loop

    if not ctx.config.api_key:
        from gabagent.config.setup import run_first_time_setup
        ctx.config = await run_first_time_setup(ctx.config)
        from gabagent.api.client import GabAIClient
        ctx.client = GabAIClient(
            api_key=ctx.config.api_key,
            base_url=ctx.config.base_url,
            model=ctx.config.model,
            rate_limiter=ctx.rate_limiter,
        )

    try:
        await run_loop(ctx, initial_prompt=prompt)
    except KeyboardInterrupt:
        pass
    finally:
        if ctx.shell_state:
            ctx.shell_state.close()


def _register_tools() -> None:
    try:
        import gabagent.tools.file_tools  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.tools.shell_tool  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.tools.search_tools  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.tools.web_tool  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.tools.git_tools  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.plan.mode  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.session.memory  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.agent.sub_agent  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.tools.bridge_tool  # noqa: F401
    except ImportError:
        pass
    try:
        import gabagent.tools.postmortem_tool  # noqa: F401
    except ImportError:
        pass
