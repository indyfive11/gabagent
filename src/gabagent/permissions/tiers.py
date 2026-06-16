"""Voice-mode safety tier classifier.

Classifies a resolved (tool, args) pair into a safety tier so the voice approval
hook can decide how much proof-of-authority to require:

    1 = auto (no gate)             — reads, and writes into the safe-write zone
    2 = spoken yes/no              — project edits, local git, plan docs
    3 = keyboard confirmation      — bash/sudo/rm/push/system/credential/etc.
    (4 = reserved, never emitted here)

This module is voice-only. It does NOT touch PermissionEngine.check — the TUI
permission flow is unchanged. The default is fail-closed (Tier 3).
"""
from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.config.models import GabAgentConfig

# Read-only tools — always Tier 1.
_TIER1_READS = {
    "read_file", "glob", "grep", "git_status", "git_diff", "git_log",
    "web_search", "web_fetch", "check_inbox", "read_claude_memory",
    # Internal bookkeeping/telemetry — must auto-run, never prompt the user.
    "postmortem_log",
    # The agent's own project memory: reading is a read; writing is reversible (the user can
    # say "forget that"), so saving a note should never gate. Keeps the growing-memory loop fluid.
    "memory_read", "memory_write",
}

# Local, reversible, non-system actions — Tier 2 (spoken yes/no).
_TIER2_TOOLS = {"git_commit", "git_add", "send_to_claude"}

# Filesystem-write tools → which arg holds the destination path.
_WRITE_PATH_ARG = {"write_file": "path", "edit": "path"}

# Always Tier 3 regardless of args (arbitrary shell).
_TIER3_TOOLS = {"bash", "run_shell"}


def _default_zones(cwd: Path, config: GabAgentConfig | None) -> list[Path]:
    """Safe-write zone for auto (Tier-1) writes. Note: cwd is intentionally NOT
    included — project edits are Tier 2 (spoken yes/no)."""
    home = Path.home()
    zones = [home / "Documents", home / "voice-scratch"]
    extra = getattr(config, "voice_safe_zones", None) or []
    for z in extra:
        zones.append(Path(z).expanduser())
    out: list[Path] = []
    for z in zones:
        try:
            out.append(z.resolve())
        except Exception:
            pass
    return out


def _resolve_dest(tool_name: str, args: dict, cwd: Path) -> Path | None:
    raw = args.get(_WRITE_PATH_ARG[tool_name])
    if not raw or not isinstance(raw, str):
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = cwd / p
    try:
        return p.resolve()
    except Exception:
        return p


def _is_sensitive(dest: Path) -> bool:
    s = str(dest)
    if s.startswith("/etc/") or s == "/etc":
        return True
    if "/.ssh/" in s or s.endswith("/.ssh"):
        return True
    # A dotfile directly under $HOME (e.g. ~/.bashrc, ~/.gitconfig).
    home = Path.home()
    if dest.parent == home and dest.name.startswith("."):
        return True
    return False


def _under(dest: Path, base: Path) -> bool:
    try:
        return dest.is_relative_to(base)
    except (ValueError, AttributeError):
        return False


def tier_of(
    tool_name: str,
    args: dict,
    cwd: Path,
    config: GabAgentConfig | None = None,
    catalog=None,
) -> int:
    """Return the safety tier (1|2|3) for a resolved tool call. Fail-closed → 3."""
    # Command framework: run_command's tier IS the catalog command's declared (effective) tier.
    if tool_name == "run_command":
        cmd = catalog.get(args.get("command_id", "")) if catalog is not None else None
        # An unknown id can't execute (the tool rejects it cleanly and the model self-corrects via
        # list_capabilities), so don't make the user keyboard-confirm a nonexistent command.
        return cmd.tier if cmd is not None else 1
    if tool_name in ("list_capabilities", "rescan_capabilities"):
        return 1

    if tool_name in _TIER1_READS:
        return 1

    if tool_name in _TIER3_TOOLS:
        return 3

    if tool_name in _WRITE_PATH_ARG:
        dest = _resolve_dest(tool_name, args, cwd)
        if dest is None:
            return 3
        if _is_sensitive(dest):
            return 3
        for zone in _default_zones(cwd, config):
            if _under(dest, zone):
                return 1
        if _under(dest, cwd.resolve()):
            return 2
        return 3

    # git_branch: listing is a read; create/switch is a local Tier-2 change.
    if tool_name == "git_branch":
        return 1 if args.get("action") == "list" else 2

    if tool_name in _TIER2_TOOLS:
        return 2

    # Reconfiguring the safe-write zone moves the safety boundary itself.
    if tool_name == "reconfigure_safe_zone":
        return 3

    # The model mis-called a COMMAND as a direct tool (e.g. `tidal.recommendations` instead of
    # run_command(command_id="tidal.recommendations")). It won't execute — the tool layer rejects the
    # unknown tool and the model retries via run_command — but it must NOT fail-closed to a Tier-3 keyboard
    # gate: resolve it to the command's catalog tier, same as run_command would. Otherwise a safe "pick &
    # play" slip gets a mouse-click prompt while the correct path is Tier-1/auto (live 2026-06-15).
    if catalog is not None:
        cmd = catalog.get(tool_name)
        if cmd is not None:
            return cmd.tier

    return 3  # fail-closed
