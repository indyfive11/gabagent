from __future__ import annotations
import asyncio
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_STARTUP_TIMEOUT = 15  # seconds


async def ensure_ollama_running(ctx: AgentContext) -> bool:
    """Ping Ollama; start it as a subprocess if not running. Returns True on success."""
    import httpx

    base = ctx.config.local_base_url.removesuffix("/v1").rstrip("/")
    health = base + "/api/tags"

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(health, timeout=2.0)
            if r.status_code == 200:
                return True
    except Exception:
        pass

    env = {**os.environ, "HSA_OVERRIDE_GFX_VERSION": "11.0.0"}
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        ctx.local_process = proc
    except FileNotFoundError:
        return False

    deadline = asyncio.get_event_loop().time() + _STARTUP_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(health, timeout=1.0)
                if r.status_code == 200:
                    return True
        except Exception:
            pass

    return False


def stop_ollama(ctx: AgentContext) -> None:
    if ctx.local_process is not None:
        ctx.local_process.terminate()
        ctx.local_process = None
