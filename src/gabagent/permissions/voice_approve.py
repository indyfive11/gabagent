"""Voice-mode approval hook: tiered, spoken/keyboard confirmation with undo snapshots,
Tier-3 arming, desktop-notification fallback, and an audit log.

Plugged into the shared tool loop via ctx.approval_hook. Signature matches the TUI's
interactive_approve: async (tool_name, args, ctx) -> bool.
"""
from __future__ import annotations
import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from gabagent.permissions.tiers import tier_of
from gabagent.voice import events

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_CONFIRM_TIMEOUT = 120  # seconds; fail-closed on expiry
_MAX_SNAPSHOT = 5 * 1024 * 1024  # 5 MB — skip undo snapshot for larger files


async def voice_approve(tool_name: str, args: dict, ctx: AgentContext) -> bool:
    emit = ctx.voice_emit
    vs = ctx.voice_session

    # Snapshot prior file content for undo (any tier, before the write runs).
    _snapshot_for_undo(tool_name, args, ctx)

    tier = tier_of(tool_name, args, ctx.cwd, ctx.config)

    if tier <= 1:
        return True

    if tier >= 4:
        if emit:
            await emit(events.blocked(tool_name, "That's not something I can do over voice yet."))
        _audit(ctx, tool_name, args, "blocked", tier, _summarize(tool_name, args, ctx))
        return False

    method = "spoken_yesno" if tier == 2 else "keyboard"
    family = _tool_family(tool_name)
    summary = _summarize(tool_name, args, ctx)

    # Tier-3 arming: a recent keyboard confirm covers related actions for a TTL.
    if method == "keyboard" and vs is not None and vs.is_armed(family):
        if emit:
            await emit(events.status("Still within the confirmed window — going ahead."))
        _audit(ctx, tool_name, args, "armed", tier, summary)
        return True

    if emit is None or vs is None:
        return False  # no channel to ask — fail closed

    reason = _reason(tool_name, args, tier, ctx)
    cid = uuid4().hex
    async with vs.lock:
        fut = vs.new_confirm(cid)
        await emit(events.confirm(cid, tier, method, summary, reason))
        if method == "keyboard":
            _desktop_notify(summary)
        try:
            approved, passphrase = await asyncio.wait_for(fut, timeout=_CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            vs.pending.pop(cid, None)
            _audit(ctx, tool_name, args, "timeout", tier, summary)
            return False

    if method == "keyboard":
        required = (getattr(ctx.config, "voice_passphrase", "") or "").strip()
        if required and (passphrase or "").strip() != required:
            _audit(ctx, tool_name, args, "denied-bad-passphrase", tier, summary)
            return False
        if approved:
            vs.arm(family, getattr(ctx.config, "voice_arm_seconds", 0) or 0)

    _audit(ctx, tool_name, args, "approved" if approved else "denied", tier, summary)
    return bool(approved)


# -- helpers ---------------------------------------------------------------

def _tool_family(tool_name: str) -> str:
    if tool_name in ("bash", "run_shell"):
        return "shell"
    if tool_name in ("write_file", "edit"):
        return "write"
    return tool_name


def _snapshot_for_undo(tool_name: str, args: dict, ctx: AgentContext) -> None:
    if tool_name not in ("write_file", "edit"):
        return
    vs = ctx.voice_session
    raw = args.get("path")
    if vs is None or not raw or not isinstance(raw, str):
        return
    from gabagent.tools.file_tools import _resolve
    try:
        p = _resolve(raw, ctx.cwd)
        if p.exists():
            if p.is_dir() or p.stat().st_size > _MAX_SNAPSHOT:
                return
            prior: bytes | None = p.read_bytes()
        else:
            prior = None
        vs.push_undo(str(p), prior)
    except Exception:
        pass


def _is_destructive(tool_name: str, args: dict) -> bool:
    if tool_name in ("bash", "run_shell"):
        cmd = str(args.get("command", ""))
        return any(tok in cmd for tok in ("rm ", "rm -", "rmdir", "shred", "mkfs", "dd "))
    return False


def _mentions_vpn(args: dict) -> bool:
    blob = " ".join(str(v) for v in args.values()).lower()
    return any(t in blob for t in ("vpn", "wg-quick", "wireguard", "vpn-full", "vpn-split"))


def _summarize(tool_name: str, args: dict, ctx: AgentContext) -> str:
    from gabagent.tools.file_tools import _resolve
    if tool_name == "write_file":
        raw = args.get("path", "")
        lines = str(args.get("content", "")).count("\n") + 1
        try:
            exists = _resolve(raw, ctx.cwd).exists()
        except Exception:
            exists = False
        name = Path(raw).name or raw
        what = "replacing all of it" if exists else "a new file"
        return f"Write {name}, about {lines} lines — {what}."
    if tool_name == "edit":
        name = Path(args.get("path", "")).name or args.get("path", "a file")
        return f"Edit one section of {name}."
    if tool_name in ("bash", "run_shell"):
        cmd = str(args.get("command", "")).strip().replace("\n", " ")
        if len(cmd) > 80:
            cmd = cmd[:80] + "…"
        tail = " This is permanent." if _is_destructive(tool_name, args) else ""
        return f"Run the command: {cmd}.{tail}"
    if tool_name == "git_commit":
        return f"Commit with the message: {args.get('message', '')}."
    arg_bits = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
    return f"{tool_name} ({arg_bits})" if arg_bits else tool_name


def _reason(tool_name: str, args: dict, tier: int, ctx: AgentContext) -> str:
    bits: list[str] = []
    # The VPN warning only applies when the brain itself is networked (cloud).
    if _mentions_vpn(args) and not ctx.local_mode:
        bits.append("Heads up — this will cut my own network connection while I'm on the cloud brain.")
    if _is_destructive(tool_name, args):
        bits.append("This can't be undone.")
    if tier == 3 and not bits:
        bits.append("This one needs keyboard confirmation.")
    return " ".join(bits)


def _desktop_notify(summary: str) -> None:
    for cmd in (
        ["notify-send", "Gab voice — confirm needed", summary],
        ["kdialog", "--passivepopup", summary, "20"],
    ):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except Exception:
            continue


def _audit(ctx: AgentContext, tool_name: str, args: dict, decision: str, tier: int, summary: str) -> None:
    path = getattr(ctx, "voice_audit_path", None)
    if not path:
        return
    sid = getattr(getattr(ctx, "voice_session", None), "session_id", "")
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": sid,
        "tool": tool_name,
        "tier": tier,
        "decision": decision,
        "summary": summary,
    }
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
