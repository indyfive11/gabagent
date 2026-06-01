"""Parse declarative TOML skill manifests into Commands, and load enabled+attested skills.

Third-party skills are DATA-ONLY: a skill may only use the data backends (shell/http/launch).
Manifests declaring code backends (python/browser) are rejected — those are first-party only.
A skill's runtime tier is the EFFECTIVE tier recorded by attestation, not its self-declared tier.
"""
from __future__ import annotations
import hashlib
import json
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from gabagent.commands.model import (
    Command, Slot, Detect, DATA_BACKENDS,
    ShellBackend, HttpBackend, LaunchBackend,
)
from gabagent.config.paths import config_dir

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


class SkillError(Exception):
    """Malformed or disallowed skill manifest — rejected (won't load)."""


@dataclass
class SkillManifest:
    id: str
    name: str
    version: str
    author: str
    description: str
    commands: list[Command]
    raw_bytes: bytes = b""


def manifest_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _build_backend(d: dict):
    kind = d.get("kind")
    if kind not in DATA_BACKENDS:
        raise SkillError(
            f"backend kind {kind!r} not allowed in a skill plugin "
            f"(only {', '.join(DATA_BACKENDS)} — code backends are first-party only)"
        )
    if kind == "shell":
        argv = d.get("argv")
        if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv) or not argv:
            raise SkillError("shell backend needs a non-empty 'argv' list of strings")
        return ShellBackend(argv=argv, timeout=float(d.get("timeout", 15.0)))
    if kind == "http":
        return HttpBackend(
            method=str(d.get("method", "GET")).upper(),
            path=str(d.get("path", "")),
            query={str(k): str(v) for k, v in (d.get("query") or {}).items()},
            json_body=d.get("json_body"),
            auth=str(d.get("auth", "")),
        )
    return LaunchBackend(target=str(d.get("target", "")))


def _build_slot(d: dict) -> Slot:
    enum = d.get("enum")
    return Slot(
        name=str(d["name"]),
        type=str(d.get("type", "string")),
        required=bool(d.get("required", False)),
        enum=tuple(str(x) for x in enum) if enum else None,
        description=str(d.get("description", "")),
        default=d.get("default"),
    )


def parse_manifest(raw: bytes | str) -> SkillManifest:
    raw_bytes = raw.encode() if isinstance(raw, str) else raw
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SkillError(f"invalid TOML: {e}") from e

    skill = data.get("skill")
    if not isinstance(skill, dict) or not skill.get("id"):
        raise SkillError("missing [skill] table with an 'id'")

    raw_cmds = data.get("command")
    if not isinstance(raw_cmds, list) or not raw_cmds:
        raise SkillError("skill declares no [[command]] entries")

    commands: list[Command] = []
    for c in raw_cmds:
        if not isinstance(c, dict):
            raise SkillError("each [[command]] must be a table")
        for k in ("id", "summary", "tier", "backend"):
            if k not in c:
                raise SkillError(f"command missing required field: {k}")
        tier = c["tier"]
        if tier not in (1, 2, 3):
            raise SkillError(f"command {c['id']}: tier must be 1, 2, or 3")
        if not isinstance(c["backend"], dict):
            raise SkillError(f"command {c['id']}: [command.backend] must be a table")
        commands.append(Command(
            id=str(c["id"]),
            domain=str(c.get("domain", skill["id"])),
            summary=str(c["summary"]),
            tier=int(tier),
            backend=_build_backend(c["backend"]),
            detect=Detect(**{k: c["detect"][k] for k in c["detect"]}) if isinstance(c.get("detect"), dict) else Detect(),
            params=[_build_slot(s) for s in (c.get("params") or [])],
            examples=[str(x) for x in (c.get("examples") or [])],
        ))

    return SkillManifest(
        id=str(skill["id"]),
        name=str(skill.get("name", skill["id"])),
        version=str(skill.get("version", "0.0.0")),
        author=str(skill.get("author", "")),
        description=str(skill.get("description", "")),
        commands=commands,
        raw_bytes=raw_bytes,
    )


def skills_root() -> Path:
    return config_dir() / "skills"


def load_enabled_skills(ctx: AgentContext) -> list[Command]:
    """Load commands from installed skills that are enabled, attested, and untampered.
    Each command's tier is overridden with the recorded EFFECTIVE tier."""
    out: list[Command] = []
    base = skills_root()
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        rec_f, toml_f = d / "attestation.json", d / "skill.toml"
        if not (rec_f.exists() and toml_f.exists()):
            continue
        try:
            rec = json.loads(rec_f.read_text(encoding="utf-8"))
            if not rec.get("enabled"):
                continue
            raw = toml_f.read_bytes()
            if manifest_hash(raw) != rec.get("manifest_hash"):
                continue  # tampered since attestation → refuse to load
            manifest = parse_manifest(raw)
            eff = rec.get("effective_tiers", {})
            for cmd in manifest.commands:
                out.append(replace(cmd, tier=int(eff.get(cmd.id, cmd.tier))))
        except Exception:
            continue  # one bad skill never breaks discovery
    return out
