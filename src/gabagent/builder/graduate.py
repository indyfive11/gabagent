"""Graduate a builder project: promote a matured sandbox project to a real home and keep it auto-run.

Flow (suggest → confirm, voice-native): with no name the caller is handed a suggested name + destination
to read back; with a name it executes — move the project dir to `<builder_graduate_root>/<name>`,
append that path to `builder_allowed_roots` (so future builds there stay Tier-1/auto), persist the
config, and re-point the registry. No keyboard — the spoken yes/no is the gate.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from gabagent.builder import projects

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def suggest_name(path: str) -> str:
    """A plausible project name derived from the code: package.json/pyproject name, the README's first
    heading, else the directory's own name."""
    p = Path(path)
    try:
        pkg = p / "package.json"
        if pkg.is_file():
            name = json.loads(pkg.read_text(encoding="utf-8")).get("name")
            if name:
                return projects.slugify(str(name), fallback=p.name)
    except Exception:
        pass
    try:
        pyproject = p / "pyproject.toml"
        if pyproject.is_file():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                m = line.strip()
                if m.startswith("name") and "=" in m:
                    val = m.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        return projects.slugify(val, fallback=p.name)
    except Exception:
        pass
    try:
        readme = next((c for c in p.iterdir() if c.name.lower().startswith("readme")), None)
        if readme and readme.is_file():
            for line in readme.read_text(encoding="utf-8").splitlines():
                if line.startswith("#"):
                    return projects.slugify(line.lstrip("#").strip(), fallback=p.name)
    except Exception:
        pass
    return projects.slugify(p.name, fallback="project")


def graduate(ctx: AgentContext, name: str | None = None) -> tuple[str, str]:
    """Promote the ACTIVE project. Moves its dir to <graduate_root>/<name>, allow-lists the new path,
    persists config, and re-points the registry/active pointer. Returns (final_name, dest_path).
    Raises ValueError with a spoken-friendly message on any precondition failure."""
    proj = projects.active()
    if proj is None:
        raise ValueError("There's no current builder project to graduate.")
    grad_root = (getattr(ctx.config, "builder_graduate_root", "") or "").strip()
    if not grad_root:
        raise ValueError("No graduation folder is configured, so I can't promote it yet.")
    src = Path(proj["path"])
    if not src.is_dir():
        raise ValueError(f"The project folder for {proj['name']} is missing.")

    final = projects.slugify(name or proj["name"], fallback=proj["name"])
    dest = Path(grad_root).expanduser() / final
    if dest.exists():
        raise ValueError(f"There's already a project at {dest}. Pick another name.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))

    # Keep the new home auto-run: append it to the allow-list, live + persisted.
    roots = list(getattr(ctx.config, "builder_allowed_roots", None) or [])
    dest_str = str(dest)
    if dest_str not in roots:
        roots.append(dest_str)
        ctx.config.builder_allowed_roots = roots   # live guardrail sees it immediately
        _persist_allowed_root(dest_str)            # targeted settings.json append

    projects.rename_path(proj["name"], final, dest_str, graduated=True)
    return final, dest_str


def _persist_allowed_root(root: str) -> None:
    """Append ONE root to builder_allowed_roots in settings.json with a TARGETED json edit, preserving
    the user's hand-curated file (a full model_dump rewrite would expand every default inline).
    Best-effort: the in-memory config update already holds for this run if the write fails."""
    try:
        import json
        from gabagent.config.paths import settings_file
        sf = settings_file()
        data = json.loads(sf.read_text()) if sf.exists() else {}
        roots = data.get("builder_allowed_roots") or []
        if root not in roots:
            roots.append(root)
            data["builder_allowed_roots"] = roots
            sf.write_text(json.dumps(data, indent=2))
    except Exception:
        pass
