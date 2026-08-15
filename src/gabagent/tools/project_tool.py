"""Voice project attach (Part B): let Aria attach to a coding project by voice ("work on X") so its
memory + Current Focus load and edits under it resolve there.

Security (from the Deep-Code pressure test):
  * A spoken name is matched against the ENUMERATED child folders of the allow-listed roots — never
    joined as a path (a spoken "dot dot slash etc" must not assemble to ``../etc``).
  * Both the root and the resolved target are ``.resolve()``-d and containment-checked with
    ``is_relative_to`` (collapses ``..``, follows symlinks, defeats prefix-siblings).
  * An empty ``voice_attachable_roots`` (default) ⇒ nothing attachable ⇒ historical no-op.
  * A root that IS ``$HOME`` / ``/`` or an ancestor of a sensitive zone is refused, so attaching can
    never relocate the Tier-2 spoken-write zone onto a credential/config tree.
These tools are voice-only (they need ``ctx.voice_session``); on the TUI they no-op with an error.
"""
from __future__ import annotations
from pathlib import Path
from typing import Any, TYPE_CHECKING

from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def _sensitive_zones() -> list[Path]:
    # Specific credential/config dirs — NOT $HOME itself (else every ~/dev, ~/projects would be
    # refused). $HOME and / are rejected explicitly in _safe_root; ancestors of $HOME (e.g. /home)
    # are still caught here because they contain these zones (`s.is_relative_to(base)`).
    home = Path.home()
    zones = [Path("/etc"), home / ".ssh", home / ".config", home / ".aws",
             home / ".gnupg", home / ".gpg", home / ".local" / "share" / "gabagent"]
    out: list[Path] = []
    for z in zones:
        try:
            out.append(z.resolve())
        except Exception:
            pass
    return out


def _safe_root(base: Path) -> bool:
    """A root is safe iff it is not ``/`` or ``$HOME`` itself, and is neither inside nor an ancestor of
    a sensitive zone — so attaching can't spoken-downgrade a credential/config tree. A normal project
    parent like ``~/dev`` IS safe (under $HOME, but not $HOME and not near a sensitive dir)."""
    if base == Path("/") or base == Path.home().resolve():
        return False
    for s in _sensitive_zones():
        try:
            if base == s or base.is_relative_to(s) or s.is_relative_to(base):
                return False
        except Exception:
            return False
    return True


def resolve_project(name: str, config: Any) -> tuple[Path | None, list[str]]:
    """Resolve a spoken project name against ``voice_attachable_roots``. Returns (target, available):
    target is the matched project root (or None), available is the sorted list of attachable names.
    Empty allow-list ⇒ (None, []) ⇒ nothing attachable."""
    roots = getattr(config, "voice_attachable_roots", None) or []
    candidates: dict[str, Path] = {}
    for r in roots:
        try:
            base = Path(r).expanduser().resolve()
        except Exception:
            continue
        if not _safe_root(base) or not base.is_dir():
            continue
        try:
            children = sorted(base.iterdir())
        except Exception:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name.startswith("."):
                    continue
                cp = child.resolve()
                if cp == base or cp.is_relative_to(base):   # containment (defeats symlink-out)
                    candidates.setdefault(child.name.lower(), cp)
            except Exception:
                continue
    available = sorted(candidates.keys())
    if not candidates:
        return None, available
    key = (name or "").strip().lower()
    if not key:
        return None, available
    target = candidates.get(key)
    if target is None:  # forgiving substring match (spoken names are fuzzy)
        for k, p in candidates.items():
            if key in k or k in key:
                target = p
                break
    return target, available


def stage_attach(vs, target) -> str:
    """Stage an attach on the voice session; return the spoken confirmation. Shared by the
    work_on_project tool AND the meta-command fast-path so both use one resolver + one message
    (keeps the Deep-Code security invariants in `resolve_project` from drifting between paths)."""
    target = Path(target)
    vs.stage_project(target)   # applied at the next turn boundary (avoids a same-round cwd race)
    return f"Okay, I'll work on {target.name}."


def stage_detach(vs) -> str:
    """Stage a detach; return the spoken line (or a no-op line if nothing is attached)."""
    if getattr(vs, "active_project", None) is None:
        return "I'm not working on a project right now."
    vs.stage_project(None)     # restores base_cwd at the next turn boundary
    return "Okay, I've stopped working on the project."


@registry.register
class WorkOnProjectTool(ToolBase):
    name = "work_on_project"
    description = (
        "Attach to a coding project so you can work on it: loads that project's saved memory and "
        "Current Focus, and points file edits at it. Use when the user says to work on / switch to a "
        "named project. Voice only. The project must be one configured as attachable."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The project name to work on (a folder name)."},
        },
        "required": ["name"],
    }

    async def execute(self, ctx: AgentContext, name: str = "", **kwargs: Any) -> ToolResult:
        vs = getattr(ctx, "voice_session", None)
        if vs is None:
            return ToolResult(output="", error="work_on_project is only available over voice.")
        target, available = resolve_project(name, ctx.config)
        if target is None:
            if not available:
                return ToolResult(output="No projects are configured for me to work on.")
            return ToolResult(
                output=f"I don't have a project called {name}. I can work on: {', '.join(available)}."
            )
        return ToolResult(output=stage_attach(vs, target))


@registry.register
class LeaveProjectTool(ToolBase):
    name = "leave_project"
    description = (
        "Stop working on the current project and go back to normal (detach). Voice only. Use when the "
        "user says to leave / stop working on the project."
    )
    parameters = {"type": "object", "properties": {}}

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        vs = getattr(ctx, "voice_session", None)
        if vs is None:
            return ToolResult(output="", error="leave_project is only available over voice.")
        return ToolResult(output=stage_detach(vs))
