"""Deterministic, offline danger pre-scan of a skill's declared backends.

This is the first (and most important) attestation layer: it never trusts the skill's
self-declared tier, catches destructive/privileged/fetch-exec commands, and — critically —
flags **obfuscation/shell-wrappers** (`bash -c`, `eval`, inline interpreters, `$(...)`,
base64) that would defeat token-by-token safety and hide an `rm -rf`. Flagged commands get a
**static-floor tier** (usually 3); obfuscation can be configured to reject outright.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field

from gabagent.commands.model import Command

_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_INTERP = {"python", "python3", "perl", "ruby", "node", "php", "awk"}

_DESTRUCTIVE = [re.compile(p, re.I) for p in (
    r"\brm\s+-[a-z]*[rf]", r"\bshred\b", r"\bmkfs", r"\bdd\s+if=", r":\(\)\s*\{",
    r">\s*/(etc|usr|boot|bin|sbin|lib|dev|sys|proc|var)\b", r"\bchmod\s+-?[a-zR]*\s*777\b",
    r"\bwipefs\b", r"\bfdisk\b", r"\bmkswap\b", r"\bchown\b[^\n]*\s/\s*$",
)]
_PRIV = [re.compile(p, re.I) for p in (
    r"\bsudo\b", r"\bdoas\b", r"\bsu\s+-", r"\bsystemctl\s+(poweroff|reboot|halt|isolate)",
    r"\b(shutdown|reboot|poweroff|halt)\b", r"\bkill\s+-9\s+-1\b", r"\bvisudo\b",
    r"\b(useradd|userdel|passwd)\b", r"\biptables\b", r"\bnft\b",
)]
_FETCH_EXEC = [re.compile(p, re.I) for p in (
    r"(curl|wget|fetch)\b[^|]*\|\s*(sh|bash|zsh|python)", r"\bnc\b[^\n]*\s-e\b",
)]
_OBFUSCATION = [re.compile(p, re.I) for p in (
    r"\bbase64\b\s+-{0,2}d", r"\bxxd\b\s+-r", r"\$\(", r"`[^`]+`", r"\beval\b",
    r"\|\s*(sh|bash|zsh)\b", r"\\x[0-9a-f]{2}",
)]
_INTERNAL_URL = re.compile(r"https?://(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|0\.0\.0\.0)", re.I)


@dataclass
class StaticVerdict:
    per: dict = field(default_factory=dict)   # cmd_id -> {"floor": int, "flags": [str]}
    reject: bool = False
    reject_reason: str = ""

    def floor(self, cid: str) -> int:
        return self.per.get(cid, {}).get("floor", 1)

    def flags(self, cid: str) -> list[str]:
        return self.per.get(cid, {}).get("flags", [])

    def all_flags(self) -> list[str]:
        return [f"{cid}: {fl}" for cid, v in self.per.items() for fl in v.get("flags", [])]


def _scan_shell(argv: list[str]) -> tuple[int, list[str], bool]:
    floor, flags, obfus = 1, [], False
    arg0 = os.path.basename(argv[0]) if argv else ""
    joined = " ".join(argv)

    if arg0 in _SHELLS and any(a == "-c" for a in argv):
        flags.append("shell wrapper (bash -c): opaque, could hide anything")
        floor, obfus = 3, True
    if arg0 in _INTERP and any(a in ("-c", "-e") for a in argv):
        flags.append("inline-code interpreter: opaque")
        floor, obfus = 3, True
    for pats, label in ((_DESTRUCTIVE, "destructive"), (_PRIV, "privileged/system"), (_FETCH_EXEC, "fetch-and-execute")):
        if any(p.search(joined) for p in pats):
            flags.append(label)
            floor = 3
    if any(p.search(joined) for p in _OBFUSCATION):
        flags.append("obfuscation tokens")
        floor, obfus = 3, True
    return floor, flags, obfus


def static_scan(commands: list[Command], auto_reject_obfuscation: bool = False) -> StaticVerdict:
    sv = StaticVerdict()
    for cmd in commands:
        b = cmd.backend
        kind = getattr(b, "kind", "")
        floor, flags, obfus = cmd.tier, [], False
        if kind == "shell":
            f, fl, o = _scan_shell(b.argv)
            floor, flags, obfus = max(floor, f), fl, o
        elif kind == "http":
            if _INTERNAL_URL.search(b.path or ""):
                flags.append("targets an internal/loopback address (SSRF risk)")
                floor = max(floor, 2)
        elif kind == "launch":
            if b.target.startswith(("/", "file://")):
                flags.append("opens a local filesystem path")
                floor = max(floor, 2)
        sv.per[cmd.id] = {"floor": floor, "flags": flags}
        if obfus and auto_reject_obfuscation:
            sv.reject = True
            sv.reject_reason = f"{cmd.id}: {flags[0] if flags else 'obfuscation'} — rejected per policy"
    return sv
