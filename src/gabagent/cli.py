from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from typing import Optional
import typer

app = typer.Typer(name="gab", help="Gab-Agent: AI coding assistant powered by Gab AI")

# Held open for the whole process so faulthandler can write to its fd at crash time.
_FAULT_LOG = None


def _enable_faulthandler() -> None:
    """Dump a C+Python stack on a FATAL native crash (segfault/abort/bus error).

    Without this, a native crash in a C/Rust extension — e.g. the ddgs/primp search backend that
    once killed the voice brain mid web-lookup — exits the process with NO Python traceback, leaving
    the death completely unexplained. Writes to a persistent file (survives the exit) so the next
    occurrence is self-identifying; falls back to stderr if the file can't be opened."""
    global _FAULT_LOG
    try:
        import faulthandler
        if faulthandler.is_enabled():
            return
        try:
            from gabagent.config.paths import data_dir
            _FAULT_LOG = open(data_dir() / "faulthandler.log", "a", buffering=1)
            faulthandler.enable(file=_FAULT_LOG, all_threads=True)
        except Exception:
            faulthandler.enable(all_threads=True)  # → stderr (the brain's goes to the journal)
    except Exception:
        pass


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
    from gabagent.api.factory import build_client
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

    # Provider-aware tier seeding: when on Claude, the bottom ladder rung is the base model and the
    # display badge's "simple" tier (mutating the runtime cfg is the existing pattern, cf. sub_agent).
    if cfg.provider == "claude" and not model:
        cfg.model = cfg.claude.ladder[0].model
    simple_model = cfg.claude.ladder[0].model if cfg.provider == "claude" else cfg.router.simple_model

    rate_limiter = UsageTracker(simple_model=simple_model)
    client = build_client(cfg, rate_limiter)

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

    persona = None
    if cfg.persona_enabled:
        try:
            from gabagent.persona.manager import PersonaManager
            persona = PersonaManager().brief() or None
        except Exception:
            persona = None

    system_prompt = build_system_prompt(
        cwd=cwd,
        memory=memory or None,
        load_global_claude_md=cfg.load_global_claude_md,
        persona=persona,
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
    ctx.local_floor = cfg.local_floor  # mirror the persisted floor pin onto the live context
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
    set_claude_key: str = typer.Option("", "--set-claude-key", help="Save an Anthropic key, switch to the Claude backend, and exit"),
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
) -> None:
    _enable_faulthandler()

    if version:
        from gabagent import __version__
        typer.echo(f"gab-agent {__version__}")
        raise typer.Exit()

    if set_claude_key:
        from gabagent.config.loader import load_config, save_config
        from gabagent.config.paths import settings_file
        cfg = load_config()
        cfg.provider = "claude"
        cfg.claude.api_key = set_claude_key.strip()
        cfg.model = cfg.claude.ladder[0].model
        save_config(cfg)
        typer.echo(f"Saved Claude backend to {settings_file()} (provider=claude, base model {cfg.model}).")
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


async def _persona_reflect(ctx) -> None:
    """Hand persona learning to a DETACHED child that outlives this process, then return at once.

    Reflection uses the top rung (opus/max) and routinely takes far longer than the ~5s the voice
    front-end gives the brain between SIGTERM and SIGKILL — so doing it in-process here loses the race
    every time and never persists (the live store stayed seed-only for days). Instead we serialize the
    recent transcript to a handoff file and spawn `python -m gabagent.persona.reflect_detached` in its
    OWN session (start_new_session=True). Killing the brain — by pid or process group — never touches
    that child; it finishes on its own time. Best-effort: never blocks shutdown, never raises.

    The _MIN_TURNS guard is checked here (in-memory, cheap) so a trivial 'hello' session never spawns a
    child; reflect_from_ctx re-applies it defensively in the child."""
    cfg = ctx.config
    if not (getattr(cfg, "persona_enabled", False) and getattr(cfg, "persona_reflect_on_shutdown", False)):
        return
    try:
        import json
        import os
        import shutil
        import subprocess
        import tempfile
        from gabagent.persona.manager import _MIN_TURNS, _TRANSCRIPT_TURNS

        turns = [m for m in ctx.session.messages() if m.role in ("user", "assistant") and m.content]
        if sum(1 for m in turns if m.role == "user") < _MIN_TURNS:
            return  # nothing worth learning from — don't spawn

        payload = {"turns": [{"role": m.role, "content": m.content} for m in turns[-_TRANSCRIPT_TURNS:]]}
        fd, path = tempfile.mkstemp(prefix="gabagent_persona_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        argv = [sys.executable, "-m", "gabagent.persona.reflect_detached", path]
        # The voice front-end runs the brain inside the `voice-agent.service` systemd cgroup
        # (Type=simple, default KillMode=control-group). A normal voice shutdown exits non-zero →
        # Restart=on-failure cycles the WHOLE cgroup, and `systemctl --user stop` SIGTERMs it outright.
        # `start_new_session=True` escapes the process GROUP but NOT the cgroup, so a plain detached child
        # is reaped on every teardown. Run reflection in its own transient systemd SCOPE so it lands in a
        # separate cgroup under the user manager and outlives the brain's cgroup being killed.
        systemd_run = shutil.which("systemd-run")
        spawned = False
        if systemd_run:
            try:
                subprocess.Popen(
                    [systemd_run, "--user", "--scope", "--quiet", "--collect", "--", *argv],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True,
                )
                spawned = True
            except Exception:
                spawned = False  # systemd-run present but unusable → fall back below
        if not spawned:
            # No systemd-run (non-systemd host / TUI dev box): best-effort process-group escape. Survives a
            # direct terminate() but NOT a systemd cgroup cycle — acceptable where there's no cgroup to dodge.
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
    except Exception:
        pass


async def _run(ctx, prompt: str | None) -> None:
    from gabagent.agent.loop import run_loop
    from gabagent.config.setup import backend_configured, run_first_time_setup

    if not backend_configured(ctx.config):
        ctx.config = await run_first_time_setup(ctx.config)
        from gabagent.api.factory import build_client
        ctx.client = build_client(ctx.config, ctx.rate_limiter)

    if ctx.config.commands_enabled:
        try:
            from gabagent.commands.discovery import discover_capabilities
            ctx.command_catalog = await discover_capabilities(ctx)
        except Exception:
            pass

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
        await _persona_reflect(ctx)  # learn from this typed session before teardown
        if ctx.shell_state:
            ctx.shell_state.close()
        if ctx.local_process is not None:
            from gabagent.local.ollama import stop_ollama
            stop_ollama(ctx)
        if ctx.voice_frontend_process is not None:
            from gabagent.voice.launcher import stop_frontend
            stop_frontend(ctx)
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
    if ctx.config.voice_debug_log:
        ctx.voice_debug_path = data_dir() / "voice_debug.jsonl"

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
    # The TUI launcher stops the brain with terminate() → SIGTERM, which by default kills the
    # process WITHOUT unwinding `_run_voice`'s finally (where persona reflection + cleanup live).
    # Convert SIGTERM into KeyboardInterrupt so a TUI-spawned brain shuts down gracefully too.
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    except (AttributeError, ValueError):
        pass

    try:
        asyncio.run(_run_voice(ctx, host="127.0.0.1", port=bind_port))
    except KeyboardInterrupt:
        pass


async def _run_voice(ctx, host: str, port: int) -> None:
    from gabagent.config.setup import backend_configured, run_first_time_setup

    if not backend_configured(ctx.config):
        ctx.config = await run_first_time_setup(ctx.config)
        from gabagent.api.factory import build_client
        ctx.client = build_client(ctx.config, ctx.rate_limiter)

    # If pinned to the local model, bring Ollama up before serving (exclusive local).
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
    # Persisted cross-backend FLOOR: bring local up WARM as the bottom rung (it stays resident, the
    # router escalates Aria→Claude). Pre-warm so the first routed turn isn't slow.
    elif ctx.config.local_floor and ctx.config.local_model:
        from gabagent.local.ollama import start_local_floor
        err = await start_local_floor(ctx)
        if err:
            typer.echo(f"Could not warm local floor: {err}", err=True)

    if ctx.config.commands_enabled:
        try:
            from gabagent.commands.discovery import discover_capabilities
            ctx.command_catalog = await discover_capabilities(ctx)
        except Exception:
            pass

    from gabagent.voice.server import serve_voice
    try:
        await serve_voice(ctx, host=host, port=port)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
    finally:
        await _persona_reflect(ctx)  # learn from this voice session before teardown (local still warm)
        if ctx.persistent_browser is not None:
            from gabagent.commands.browser import close_browser
            await close_browser(ctx)
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
    try:
        import gabagent.commands.tools  # noqa: F401
    except ImportError:
        pass
