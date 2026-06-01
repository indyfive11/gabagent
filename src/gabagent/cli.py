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

    system_prompt = build_system_prompt(
        cwd=cwd,
        memory=memory or None,
        load_global_claude_md=cfg.load_global_claude_md,
    )

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
    voice_serve: bool = typer.Option(False, "--voice-serve", help="Run as a voice brain (HTTP+SSE server)"),
    port: int = typer.Option(0, "--port", help="Voice server port (default: config voice_port)"),
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

    if voice_serve:
        _start_voice(ctx, model=model, port=port)
        raise typer.Exit()

    if not headless:
        from gabagent.session.postmortem import PostMortemManager
        pm = PostMortemManager(cwd=ctx.cwd)
        pm.check_for_crashes()

        from gabagent import __version__
        badge = (

            ctx.rate_limiter.forced_badge(ctx.config.model)
            if ctx.force_model
            else ctx.rate_limiter.badge
        )
        from gabagent.session.manager import SessionManager
        mgr = SessionManager(ctx.cwd)
        session_name = mgr.get_session_name(ctx.session_id)
        name_display = f" [dim cyan]'{session_name}'[/dim cyan]" if session_name else ""
        console.print(
            f"[gab.accent]◆ Gab[/gab.accent] [dim]v{__version__}[/dim]  "
            f"session=[dim]{ctx.session_id[:8]}[/dim]{name_display}  "
            f"[dim]{badge}[/dim]",
            markup=True,
        )
        console.print("[dim]  /help · /tools · Ctrl-D to exit[/dim]", markup=True)

    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except AttributeError:
        pass  # Windows has no SIGPIPE

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
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    except Exception:
        import sys
        import traceback
        from gabagent.session.postmortem import PostMortemManager

        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

        pm = PostMortemManager(cwd=ctx.cwd)
        pm.log_crash(description=f"Unhandled agent loop exception:\n{tb_text}")

        raise

    finally:
        if ctx.shell_state:
            ctx.shell_state.close()
        if ctx.local_process is not None:
            from gabagent.local.ollama import stop_ollama
            stop_ollama(ctx)
        if ctx.voice_process is not None:
            from gabagent.voice.launcher import stop_brain
            stop_brain(ctx)


def _start_voice(ctx, model: str, port: int) -> None:
    from gabagent.config.paths import data_dir
    from gabagent.permissions.voice_approve import voice_approve

    ctx.voice_mode = True
    ctx.headless = True
    ctx.approval_hook = voice_approve
    ctx.voice_audit_path = data_dir() / "voice_audit.jsonl"

    # Pin a single model when --model or config.voice_model is set; otherwise leave
    # the router enabled (default arya base, escalate to Claude).
    pinned = model or ctx.config.voice_model
    if pinned:
        ctx.force_model = True
        ctx.config.model = pinned
        ctx.client.model = pinned
    else:
        ctx.force_model = False

    bind_port = port or ctx.config.voice_port
    typer.echo(f"Voice brain listening on http://127.0.0.1:{bind_port}  (Ctrl-C to stop)")

    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except AttributeError:
        pass

    try:
        asyncio.run(_run_voice(ctx, host="127.0.0.1", port=bind_port))
    except KeyboardInterrupt:
        pass


async def _run_voice(ctx, host: str, port: int) -> None:
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

    # If pinned to the local model, bring Ollama up before serving.
    if ctx.config.voice_model and ctx.config.voice_model == ctx.config.local_model:
        from gabagent.local.ollama import ensure_ollama_running
        from gabagent.api.client import GabAIClient
        err = await ensure_ollama_running(ctx)
        if err:
            typer.echo(f"Could not start local model: {err}", err=True)
        else:
            ctx.local_client = GabAIClient(
                api_key="ollama",
                base_url=ctx.config.local_base_url,
                model=ctx.config.local_model,
                rate_limiter=ctx.rate_limiter,
                keep_alive="1m",
            )
            ctx.local_mode = True

    from gabagent.voice.server import serve_voice
    try:
        await serve_voice(ctx, host=host, port=port)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
    finally:
        if ctx.config.local_model:
            from gabagent.local.ollama import unload_local
            await unload_local(ctx)  # free VRAM immediately on shutdown
        if ctx.shell_state:
            ctx.shell_state.close()
        if ctx.local_process is not None:
            from gabagent.local.ollama import stop_ollama
            stop_ollama(ctx)


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
    try:
        import gabagent.tools.claude_memory_tool  # noqa: F401
    except ImportError:
        pass
