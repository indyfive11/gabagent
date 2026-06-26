"""Builder project registry + the active-project pointer — the 'cwd' of the builder VUI.

The voice/typed builder works against ONE *current* project at a time; the user hops between them by
name ("switch builder to tetris") the way they'd `cd`. This module is the small, flock'd, on-disk
registry behind that: name → {path, description, graduated, timestamps} plus which one is active.

Two project homes feed the registry:
  - the SANDBOX (`builder_scratch_root`, e.g. ~/builder) — where "new builder project" / a path-less
    dispatch lands, auto-run because the sandbox is allow-listed;
  - GRADUATED projects (moved out to `builder_graduate_root`, e.g. ~/dev/<name>) — appended to the
    allow-list on graduation so they stay auto-run.

`list_all()` merges the registry with any sandbox dirs found on disk (so a folder made out-of-band
still shows up). Everything is deterministic — the voice meta-commands call straight into here, no LLM.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from gabagent.config.paths import data_dir

_LOCK_TIMEOUT = 5.0


def _registry_path() -> Path:
    p = data_dir() / "builder"
    p.mkdir(parents=True, exist_ok=True)
    return p / "projects.json"


def slugify(text: str, fallback: str = "project") -> str:
    """Filesystem-safe, spoken-name-friendly slug: lowercase words joined by dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if len(s) > 40:
        s = s[:40].rstrip("-")
    return s or fallback


def _empty() -> dict[str, Any]:
    return {"active": None, "projects": {}}


def _read() -> dict[str, Any]:
    p = _registry_path()
    if not p.exists():
        return _empty()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "projects" not in data:
            return _empty()
        return data
    except Exception:
        return _empty()


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _mutate(fn):
    """Read-modify-write the registry under an exclusive flock on a sidecar lock. `fn(data)` mutates in
    place and may return a value, which is returned to the caller."""
    p = _registry_path()
    lock = p.with_suffix(".lock")
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.time() + _LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    raise TimeoutError("builder registry lock contended")
                time.sleep(0.05)
        data = _read()
        result = fn(data)
        _atomic_write(p, data)
        return result
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# -- queries ---------------------------------------------------------------

def get(name: str) -> dict | None:
    rec = _read()["projects"].get(name)
    return {"name": name, **rec} if rec else None


def active() -> dict | None:
    data = _read()
    name = data.get("active")
    if name and name in data["projects"]:
        return {"name": name, **data["projects"][name]}
    return None


def list_all(scratch_root: str = "") -> list[dict]:
    """Registry entries merged with any on-disk sandbox dirs not yet registered. Most-recent first."""
    data = _read()
    out: dict[str, dict] = {}
    for name, rec in data["projects"].items():
        out[name] = {"name": name, **rec}
    root = Path(scratch_root).expanduser() if scratch_root else None
    if root and root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name not in out:
                out[child.name] = {
                    "name": child.name, "path": str(child), "description": "",
                    "graduated": False, "created_ts": 0.0, "last_ts": 0.0,
                }
    items = list(out.values())
    items.sort(key=lambda r: r.get("last_ts") or r.get("created_ts") or 0.0, reverse=True)
    return items


def resolve(query: str, scratch_root: str = "") -> tuple[str, Any]:
    """Map a spoken name to a project. Returns:
      ("ok", project)            — a single confident match (exact, slug, or unique substring)
      ("ambiguous", [projects])  — several plausible matches; caller asks which
      ("none", [projects])       — no match; caller lists what exists
    """
    items = list_all(scratch_root)
    if not items:
        return ("none", [])
    q = slugify(query)
    if not q:
        return ("none", items)
    exact = [p for p in items if p["name"] == q]
    if exact:
        return ("ok", exact[0])
    subs = [p for p in items if q in p["name"] or p["name"] in q]
    if len(subs) == 1:
        return ("ok", subs[0])
    if len(subs) > 1:
        return ("ambiguous", subs)
    return ("none", items)


# -- mutations -------------------------------------------------------------

def register(name: str, path: str, description: str = "", graduated: bool = False,
             make_active: bool = True) -> dict:
    """Insert/update a project record (preserving created_ts + a non-empty description)."""
    def _fn(data):
        now = time.time()
        rec = data["projects"].get(name, {})
        rec["path"] = path
        if description or not rec.get("description"):
            rec["description"] = description or rec.get("description", "")
        rec["graduated"] = graduated or rec.get("graduated", False)
        rec.setdefault("created_ts", now)
        rec["last_ts"] = now
        data["projects"][name] = rec
        if make_active:
            data["active"] = name
        return {"name": name, **rec}
    return _mutate(_fn)


def set_active(name: str) -> bool:
    def _fn(data):
        if name in data["projects"]:
            data["active"] = name
            return True
        return False
    return _mutate(_fn)


def touch(name: str, description: str | None = None) -> None:
    """Bump last_ts; fill the description only if it was empty (the first task names the project)."""
    def _fn(data):
        rec = data["projects"].get(name)
        if rec is None:
            return
        rec["last_ts"] = time.time()
        if description and not rec.get("description"):
            rec["description"] = description
    _mutate(_fn)


def rename_path(name: str, new_name: str, new_path: str, graduated: bool = True) -> None:
    """Move a registry entry to a new name+path (used by graduation)."""
    def _fn(data):
        rec = data["projects"].pop(name, None) or {}
        rec["path"] = new_path
        rec["graduated"] = graduated
        rec["last_ts"] = time.time()
        rec.setdefault("created_ts", time.time())
        data["projects"][new_name] = rec
        if data.get("active") == name:
            data["active"] = new_name
    _mutate(_fn)


def effective_target_path(project: str | None, name: str | None, cwd, config) -> "Path":
    """The directory a `send_to_builder` call WILL target — computed SIDE-EFFECT-FREE so the voice tier
    gate (permissions) and the tool agree exactly (omitted `project` must resolve to the current sandbox
    project, NOT the brain's cwd, or the guardrail keyboard-gates the very hands-free path it's meant to
    auto-run). Mirrors SendToBuilderTool.execute's resolution: explicit project > named sandbox >
    current project > auto sandbox > cwd."""
    from gabagent.voice.spoken_tokens import maybe_assemble_path, normalize_name
    scratch_root = (getattr(config, "builder_scratch_root", "") or "").strip()
    if project:
        p = Path(maybe_assemble_path(project)).expanduser()
    elif name and scratch_root:
        p = Path(scratch_root).expanduser() / slugify(normalize_name(name))
    else:
        act = active()
        if act is not None:
            p = Path(act["path"])
        elif scratch_root:
            p = Path(scratch_root).expanduser()   # auto-sandbox: the new child lives under this root
        else:
            p = Path(cwd)
    if not p.is_absolute():
        p = Path(cwd) / p
    try:
        return p.resolve()
    except Exception:
        return p


def new_sandbox_project(name: str, scratch_root: str, description: str = "") -> dict:
    """Create <scratch_root>/<name> (git-init'd so it's a real project home) and register+activate it.
    Returns the project record. Raises if no scratch_root is configured."""
    if not scratch_root:
        raise ValueError("no builder_scratch_root configured")
    slug = slugify(name)
    root = Path(scratch_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / slug
    dest.mkdir(parents=True, exist_ok=True)
    return register(slug, str(dest), description=description)
