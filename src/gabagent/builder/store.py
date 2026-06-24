"""Flock'd, atomic job store for the builder (one JSON file per job).

The runner (a detached process) and the TUI both touch a job — the runner writes status transitions,
the TUI flips `delivered` — so every read-modify-write happens under an exclusive flock on a sidecar
`.lock` (distinct inode, never the data file `os.replace` swaps), and writes are atomic (tmp +
`os.replace`). Job files are mode 0600 — a task/summary can carry sensitive paths or code.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from gabagent.config.paths import data_dir

_LOCK_TIMEOUT = 5.0


def builder_dir() -> Path:
    p = data_dir() / "builder" / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _job_path(job_id: str) -> Path:
    return builder_dir() / f"{job_id}.json"


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def new_job(task: str, project: str, model: str | None = None, git_mode: str = "repo") -> dict[str, Any]:
    job_id = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
    job: dict[str, Any] = {
        "id": job_id,
        "status": "queued",            # queued -> running -> done | failed
        "task": task,
        "project": project,
        "model": model,
        "git_mode": git_mode,          # repo (existing git tree) | init (auto-init one) | scratch (gitless)
        "created_ts": time.time(),
        "started_ts": None,
        "finished_ts": None,
        "exit_code": None,
        "base_commit": None,
        "base_branch": None,
        "final_branch": None,
        "commits": [],                 # ground truth: commits made on top of base
        "files_changed": [],           # ground truth: committed + working-tree changes
        "uncommitted": False,
        "self_report": "",             # the model's own final text (NOT trusted for facts)
        "summary": "",                 # ground-truth summary surfaced to the user
        "error": None,
        "delivered": False,            # surfaced in the TUI yet?
    }
    _atomic_write(_job_path(job_id), job)
    return job


def load_job(job_id: str) -> dict | None:
    p = _job_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_job(job_id: str, **fields: Any) -> dict | None:
    """Read-modify-write under an exclusive lock so a runner status write and a TUI delivered-flag
    write can't clobber each other. Returns the updated job, or None if missing / lock contended."""
    p = _job_path(job_id)
    lock = p.with_suffix(".lock")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    except Exception:
        return None
    try:
        deadline = time.time() + _LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.time() >= deadline:
                    return None  # contended → caller retries; convergent
                time.sleep(0.05)
        job = load_job(job_id)
        if job is None:
            return None
        job.update(fields)
        _atomic_write(p, job)
        return job
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def list_jobs() -> list[dict]:
    out = []
    for f in builder_dir().glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    out.sort(key=lambda j: j.get("created_ts", 0))
    return out


def undelivered_done() -> list[dict]:
    """Finished (done|failed) jobs the TUI hasn't surfaced yet."""
    return [
        j for j in list_jobs()
        if j.get("status") in ("done", "failed") and not j.get("delivered")
    ]
