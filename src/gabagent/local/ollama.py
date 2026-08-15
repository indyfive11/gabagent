from __future__ import annotations
import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_STARTUP_TIMEOUT = 60  # seconds — ROCm GPU discovery takes ~30s on cold start


def _local_subprocess_env(ctx: AgentContext) -> dict[str, str]:
    """Environment for the spawned `ollama serve`: the inherited env plus the user-configured
    overlay (`local_env`). Default empty ⇒ nothing injected, the universal-safe behavior (a generic
    install gets Ollama's own defaults, never a GPU override that fits only one card). A ROCm box
    whose arch needs spoofing sets e.g. {"HSA_OVERRIDE_GFX_VERSION": "11.0.0"} in config — written
    once by the Local-backend setup, then editable. Replaces the former hard-coded HSA_OVERRIDE."""
    return {**os.environ, **ctx.config.local_env}


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

    env = _local_subprocess_env(ctx)
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


async def enter_offline_local(ctx: AgentContext) -> str | None:
    """Fail over to the local model because the cloud is unreachable (internet outage).

    Unlike the manual `switch_to_local`, this does NO cloud summary call (the cloud is down — that call
    would just time out and stall the failover); the recent message window the local model already
    receives carries the context. Starts Ollama on demand (Rob's choice: no warm floor), builds the local
    client if needed, and routes the CURRENT turn onto local by setting active_backend="local". Sets
    `offline_failover` so the per-turn recovery probe knows to revert when the connection returns.
    Returns None on success, else an error string (Ollama wouldn't start)."""
    if not ctx.config.local_model:
        return "no local model is configured"
    err = await ensure_ollama_running(ctx)
    if err:
        return err
    if ctx.local_client is None:
        ctx.local_client = _build_local_client(ctx, "1m")  # on-demand idle-unload, not a warm floor
    ctx.clients["local"] = ctx.local_client
    ctx.local_mode = True
    ctx.offline_failover = True
    # Route this turn (and the retry that follows the failover) straight to local.
    ctx.active_model, ctx.active_effort, ctx.active_backend = ctx.config.local_model, None, "local"
    from gabagent.voice.debuglog import dlog
    dlog(ctx, "offline_failover", on=True, model=ctx.config.local_model)
    return None


async def exit_offline_local(ctx: AgentContext) -> None:
    """Revert an offline failover once the cloud is reachable again: leave local mode, free the local
    VRAM, and clear the per-turn routing so the next turn re-routes through the cloud ladder normally."""
    ctx.local_mode = False
    ctx.offline_failover = False
    ctx.cloud_probe_after = 0.0        # episode over — a future outage re-arms the probe from scratch
    ctx.active_model = ctx.active_effort = ctx.active_backend = None
    await unload_local(ctx)
    from gabagent.voice.debuglog import dlog
    dlog(ctx, "offline_failover", on=False, recovered=True)


async def unload_local(ctx: AgentContext) -> None:
    """Evict the local model from VRAM immediately (keep_alive: 0) without stopping the
    Ollama server. Best-effort — used when leaving local mode or on shutdown so the GPU is
    reclaimed without waiting out the idle timer. Safe to call even if Ollama isn't running."""
    model = ctx.config.local_model
    if not model:
        return
    import httpx

    base = ctx.config.local_base_url.removesuffix("/v1").rstrip("/")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                base + "/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=5.0,
            )
    except Exception:
        pass
    from gabagent.voice.debuglog import dlog
    dlog(ctx, "unload", model=model)


def stop_ollama(ctx: AgentContext) -> None:
    if ctx.local_process is not None:
        ctx.local_process.terminate()
        ctx.local_process = None


# Long keep_alive so the warm FLOOR model stays resident (no per-turn reload). The exclusive
# local_mode keeps the short "1m" idle-unload; the floor is meant to stay loaded.
_FLOOR_KEEP_ALIVE = "-1"


def _build_local_client(ctx: AgentContext, keep_alive: str):
    from gabagent.api.client import GabAIClient
    return GabAIClient(
        api_key="ollama",
        base_url=ctx.config.local_base_url,
        model=ctx.config.local_model,
        rate_limiter=ctx.rate_limiter,
        keep_alive=keep_alive,
    )


async def start_local_floor(ctx: AgentContext, *, prewarm: bool = True) -> str | None:
    """Make the local model the persisted, WARM bottom rung of the escalation ladder.

    Starts Ollama, builds a long-keep_alive local client, optionally pre-warms it (so the first
    real turn isn't slow), and persists `local_floor`. Returns None on success, else an error
    string. The router then assembles local → Aria → Claude; trivial turns run on local.
    """
    if not ctx.config.local_model:
        return "no local model is configured (set local_model in settings.json)"
    err = await ensure_ollama_running(ctx)
    if err:
        return err
    client = _build_local_client(ctx, _FLOOR_KEEP_ALIVE)
    ctx.local_client = client
    ctx.clients["local"] = client
    ctx.local_floor = True
    ctx.config.local_floor = True
    if prewarm:
        # One tiny throwaway call to pull the model into VRAM now, so the first routed turn is warm.
        from gabagent.api.models import ChatMessage
        try:
            await client.complete_simple([ChatMessage(role="user", content="hi")])
        except Exception:
            pass  # pre-warm is best-effort; the first real turn will load it if this missed
    from gabagent.config.loader import save_config
    save_config(ctx.config)
    from gabagent.voice.debuglog import dlog
    dlog(ctx, "local_floor", on=True, model=ctx.config.local_model)
    return None


async def stop_local_floor(ctx: AgentContext) -> None:
    """Drop local as the floor (Aria takes over) and FREE its VRAM. Persists local_floor=False.
    Re-enabling later pays the one-time reload."""
    await unload_local(ctx)
    ctx.local_floor = False
    ctx.config.local_floor = False
    ctx.local_client = None
    ctx.clients.pop("local", None)
    from gabagent.config.loader import save_config
    save_config(ctx.config)
    from gabagent.voice.debuglog import dlog
    dlog(ctx, "local_floor", on=False)
