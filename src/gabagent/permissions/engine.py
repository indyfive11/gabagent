from __future__ import annotations
import re
from enum import Enum
from fnmatch import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.config.models import GabAgentConfig


class Decision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


_DANGEROUS_PATTERNS = [
    r"rm\s+-[rf]+\s+/",
    r"rm\s+--recursive.*\s+/",
    r">\s*/etc/",
    r"git\s+push\s+--force\s+.*\s+main",
    r"git\s+push\s+--force\s+.*\s+master",
    r"dd\s+if=",
    r"mkfs\.",
    r":\(\)\{.*\}",
    r"chmod\s+777\s+/",
]
_DANGEROUS_RE = [re.compile(p) for p in _DANGEROUS_PATTERNS]


def _matches_rule(rule: str, tool_name: str, args: dict) -> bool:
    if "(" in rule:
        tool_part, rest = rule.split("(", 1)
        pattern = rest.rstrip(")")
        if not fnmatch(tool_name, tool_part):
            return False
        args_str = " ".join(str(v) for v in args.values())
        return fnmatch(args_str, f"*{pattern}*") or fnmatch(args_str, pattern)
    return fnmatch(tool_name, rule)


def _is_dangerous(tool_name: str, args: dict) -> bool:
    if tool_name not in ("bash", "run_shell"):
        return False
    cmd = args.get("command", "")
    return any(p.search(cmd) for p in _DANGEROUS_RE)


class PermissionEngine:
    def __init__(self, config: GabAgentConfig):
        self._config = config
        self._session_allow: list[str] = []
        self._session_deny: list[str] = []

    def add_session_allow(self, rule: str) -> None:
        self._session_allow.append(rule)

    def add_session_deny(self, rule: str) -> None:
        self._session_deny.append(rule)

    def check(self, tool_name: str, args: dict) -> Decision:
        mode = self._config.permissions.mode

        if mode == "bypass":
            return Decision.ALLOW

        # Deny list (config + session)
        for rule in list(self._config.permissions.deny) + self._session_deny:
            if _matches_rule(rule, tool_name, args):
                return Decision.DENY

        # Always prompt for dangerous ops regardless of allow list
        if _is_dangerous(tool_name, args):
            return Decision.PROMPT

        # Allow list (config + session)
        for rule in list(self._config.permissions.allow) + self._session_allow:
            if _matches_rule(rule, tool_name, args):
                return Decision.ALLOW

        if mode == "auto_approve":
            return Decision.ALLOW

        return Decision.PROMPT
