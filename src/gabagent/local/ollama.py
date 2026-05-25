from __future__ import annotations
import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_STARTUP_TIMEOUT = 60  # seconds — ROCm GPU discovery takes ~30s on cold start


async def ensure_ollama_running(ctx: AgentContext) -> str | None:
    """Ping Ollama; start it as a subprocess if not running.
    Returns None on success, or an error string describing the failure."""
    import httpx

    base = ctx.config.local_base_url.removesuffix("/v1").rstrip("/")
    health = base + "/api/tags"

    async def _ping() -> bool:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(health, timeout=2.0)
                return r.status_code == 200
        except Exception:
            return False

    if await _ping():
        return None

    env = {**os.environ, "HSA_OVERRIDE_GFX_VERSION": "11.0.0"}
    log_fh = Path("/tmp/ollama.log").open("w")
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=log_fh,
            stderr=log_fh,
            env=env,
        )
        ctx.local_process = proc
    except FileNotFoundError:
        return "ollama binary not found — is ollama-rocm installed?"
    finally:
        log_fh.close()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STARTUP_TIMEOUT
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        if proc.poll() is not None:
            return f"ollama serve exited with code {proc.returncode} (port already in use?)"
        if await _ping():
            return None

    return f"ollama did not respond within {_STARTUP_TIMEOUT}s — check /tmp/ollama.log"


def stop_ollama(ctx: AgentContext) -> None:
    if ctx.local_process is not None:
        ctx.local_process.terminate()
        ctx.local_process = None
