"""Launch the voice brain (`gab --voice-serve`) as a child of the TUI.

Idempotent connect-or-spawn: if a brain already answers /health on the port, we attach
(and never tear it down); otherwise we spawn one and own its lifecycle. Mirrors the
Popen-to-logfile + health-poll shape of local/ollama.py::ensure_ollama_running.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from gabagent.config.paths import data_dir

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_STARTUP_TIMEOUT = 15  # seconds to wait for the spawned brain's /health


async def brain_health(base_url: str, timeout: float = 2.0) -> bool:
    """True if a voice brain answers GET {base_url}/health with 200."""
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(base_url.rstrip("/") + "/health", timeout=timeout)
            return r.status_code == 200
    except Exception:
        return False


async def start_brain(ctx: AgentContext, port: int) -> tuple[bool, bool, str]:
    """Ensure a voice brain is running on `port`.

    Returns (running, spawned_by_us, message). If a brain is already up we attach
    (spawned_by_us=False) and do not set ctx.voice_process — so we never kill it.
    """
    base = f"http://127.0.0.1:{port}"

    if await brain_health(base):
        return True, False, "already running — attached"

    cmd = [
        sys.executable, "-m", "gabagent",
        "--voice-serve", "--port", str(port), "--cwd", str(ctx.cwd),
    ]
    if ctx.session_id:
        cmd += ["--resume", ctx.session_id]  # continue the TUI's conversation

    log_path = data_dir() / "voice-serve.log"
    log_fh = open(log_path, "w")
    try:
        proc = subprocess.Popen(
            cmd, stdout=log_fh, stderr=log_fh, env=os.environ.copy()
        )
        ctx.voice_process = proc
    except Exception as e:  # pragma: no cover - sys.executable always exists
        return False, False, f"failed to launch: {e}"
    finally:
        log_fh.close()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_TIMEOUT
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        if proc.poll() is not None:
            return False, True, f"voice brain exited (code {proc.returncode}) — check {log_path}"
        if await brain_health(base):
            return True, True, "started"

    return False, True, f"no /health within {_STARTUP_TIMEOUT}s — check {log_path}"


def stop_brain(ctx: AgentContext) -> None:
    """Terminate a brain we spawned. No-op if we only attached (voice_process is None)."""
    if ctx.voice_process is not None:
        try:
            ctx.voice_process.terminate()
        except Exception:
            pass
        ctx.voice_process = None


def _resolve_frontend_cmd(ctx: AgentContext) -> list[str] | None:
    """The command that launches the voice-agent front-end. Config override wins; else the
    `voice-agent` binary on PATH; else ~/dev/voice-agent/run.sh. Returns None if none found.

    The `gab` argument selects BRAIN=gabagent in run.sh, so the front-end talks to our brain.
    """
    configured = list(getattr(ctx.config, "voice_agent_cmd", []) or [])
    if configured:
        return configured

    onpath = shutil.which("voice-agent")
    if onpath:
        return [onpath, "gab"]

    run_sh = Path.home() / "dev" / "voice-agent" / "run.sh"
    if run_sh.exists():
        return [str(run_sh), "gab"]

    return None


async def start_frontend(ctx: AgentContext, port: int) -> tuple[bool, str]:
    """Spawn the voice-agent front-end (mic + wake word) so the brain can hear.

    Pointed at the brain on `port` via GAB_PORT, so it ATTACHES to our brain instead of
    spawning its own (symmetric with this launcher). Returns (started, message).
    """
    if ctx.voice_frontend_process is not None and ctx.voice_frontend_process.poll() is None:
        return True, "already running"

    cmd = _resolve_frontend_cmd(ctx)
    if not cmd:
        return False, (
            "voice-agent not found — install it, put it on PATH, or set "
            "`voice_agent_cmd` in settings.json"
        )

    env = os.environ.copy()
    env["GAB_PORT"] = str(port)
    env["BRAIN"] = "gabagent"  # belt-and-braces; run.sh's `gab` arg sets this too

    log_path = data_dir() / "voice-frontend.log"
    log_fh = open(log_path, "w")
    try:
        proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, env=env)
        ctx.voice_frontend_process = proc
    except Exception as e:
        return False, f"failed to launch: {e}"
    finally:
        log_fh.close()

    # No /health endpoint on the front-end — just confirm it didn't immediately die.
    await asyncio.sleep(1.0)
    if proc.poll() is not None:
        return False, f"front-end exited (code {proc.returncode}) — check {log_path}"
    return True, "started"


def stop_frontend(ctx: AgentContext) -> None:
    """Terminate a front-end we spawned. No-op if we never started one."""
    if ctx.voice_frontend_process is not None:
        try:
            ctx.voice_frontend_process.terminate()
        except Exception:
            pass
        ctx.voice_frontend_process = None
