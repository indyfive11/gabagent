"""Execute a Command's backend. Slot values are validated then substituted as WHOLE
tokens into argv lists / httpx params — they never reach a shell, so a value like
'; rm -rf ~' is just a harmless single argv element.
"""
from __future__ import annotations
import asyncio
import importlib
import re
from typing import TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.commands.model import Command

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_SLOT_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")
_HTTP_TIMEOUT = 20.0


def _coerce(slot, val):
    t = slot.type
    if t == "integer":
        return int(val)
    if t == "number":
        return float(val)
    if t == "boolean":
        return bool(val)
    if t == "enum":
        s = str(val)
        if slot.enum and s not in slot.enum:
            raise ValueError(f"{slot.name} must be one of {', '.join(slot.enum)}")
        return s
    return str(val)


def validate_and_resolve(cmd: Command, args: dict) -> dict:
    """Validate provided args against the command's slots; return {name: coerced value}.
    Rejects unknown params and missing required ones."""
    provided = dict(args or {})
    resolved: dict = {}
    for slot in cmd.params:
        if slot.name in provided:
            val = provided.pop(slot.name)
        elif slot.required:
            raise ValueError(f"missing required parameter: {slot.name}")
        elif slot.default is not None:
            val = slot.default
        else:
            continue
        resolved[slot.name] = _coerce(slot, val)
    # A command that declares NO params takes a fixed action (e.g. volume_up = pactl +10%); the model
    # sometimes hands it a stray arg anyway (`volume_up(level=50)`). Ignore extras on a paramless command
    # rather than failing the turn. Commands that DO declare params stay strict — a stray arg there is a
    # real typo (wrong name / wrong command) worth surfacing.
    if provided and cmd.params:
        raise ValueError(f"unknown parameter(s): {', '.join(provided)}")
    return resolved


def _subst(s: str, resolved: dict) -> str:
    """Replace {slot} occurrences with their values. Result stays a single string token."""
    return _SLOT_RE.sub(lambda m: str(resolved.get(m.group(1), m.group(0))), s)


async def run_backend(cmd: Command, args: dict, ctx: AgentContext) -> ToolResult:
    try:
        resolved = validate_and_resolve(cmd, args)
    except ValueError as e:
        return ToolResult(output="", error=f"Invalid arguments: {e}")

    kind = getattr(cmd.backend, "kind", "")
    if kind == "shell":
        return await _run_shell(cmd.backend, resolved)
    if kind == "http":
        return await _run_http(cmd.backend, resolved, ctx)
    if kind == "launch":
        return await _run_launch(cmd.backend, resolved)
    if kind in ("python", "browser"):
        return await _run_ref(cmd.backend, resolved, ctx)
    return ToolResult(output="", error=f"Unsupported backend: {kind!r}")


async def _run_shell(b, resolved) -> ToolResult:
    argv = [_subst(tok, resolved) for tok in b.argv]
    if not argv:
        return ToolResult(output="", error="empty command")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=b.timeout)
    except FileNotFoundError:
        return ToolResult(output="", error=f"command not found: {argv[0]}")
    except asyncio.TimeoutError:
        return ToolResult(output="", error="command timed out")
    rc = proc.returncode or 0
    text = out.decode(errors="replace").strip()
    errtext = err.decode(errors="replace").strip()
    return ToolResult(output=text or "(done)", error=errtext if rc else None, exit_code=rc)


def _auth(profile: str, ctx: AgentContext) -> tuple[str, dict]:
    """Return (base_url, headers) for a named auth profile. Empty profile => no base/headers
    (the backend path is then expected to be a full URL). Profiles added per provider (Phase 2)."""
    if profile == "jellyfin":
        jc = getattr(ctx.config, "jellyfin", None)
        if jc is not None:
            return jc.base_url, ({"Authorization": f'MediaBrowser Token="{jc.api_key}"'} if jc.api_key else {})
    return "", {}


async def _run_http(b, resolved, ctx) -> ToolResult:
    import httpx

    base_url, headers = _auth(b.auth, ctx)
    path = _subst(b.path, resolved)
    url = (base_url.rstrip("/") + path) if base_url else path
    params = {k: _subst(v, resolved) for k, v in b.query.items()}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.request(b.method, url, params=params, json=b.json_body, headers=headers)
    except Exception as e:
        return ToolResult(output="", error=f"request failed: {e}")
    body = resp.text
    return ToolResult(
        output=body[:4000],
        error=None if resp.is_success else f"HTTP {resp.status_code}",
        exit_code=0 if resp.is_success else 1,
    )


async def _run_launch(b, resolved) -> ToolResult:
    target = _subst(b.target, resolved)
    if not target:
        return ToolResult(output="", error="nothing to launch")
    try:
        await asyncio.create_subprocess_exec(
            "xdg-open", target, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
    except FileNotFoundError:
        return ToolResult(output="", error="xdg-open not found")
    return ToolResult(output=f"Opened {target}")


async def _run_ref(b, resolved, ctx) -> ToolResult:
    """Dispatch a first-party python/browser backend: 'module:callable' invoked as
    await fn(ctx, **slots). Only reachable for first-party providers (plugins can't declare these)."""
    ref = getattr(b, "ref", "")
    if ":" not in ref:
        return ToolResult(output="", error=f"bad backend ref: {ref!r}")
    mod_name, fn_name = ref.split(":", 1)
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name)
    except Exception as e:
        return ToolResult(output="", error=f"backend not found ({ref}): {e}")
    result = await fn(ctx, **resolved)
    return result if isinstance(result, ToolResult) else ToolResult(output=str(result))
