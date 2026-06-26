"""Builder lifecycle ops behind the 'cancel the build' / 'discard that build' voice verbs.

Pure-ish helpers (no LLM): stop a running detached build, or revert a scratch project's uncommitted
changes. Kept out of the runner so both the voice meta-handler and the typed tool call the same code.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time

from gabagent.builder import projects, store


def _running_jobs() -> list[dict]:
    return [j for j in store.list_jobs() if j.get("status") in ("queued", "running")]


def cancel_running(project_path: str | None = None) -> tuple[str, str | None]:
    """Stop the most recent in-flight build (optionally scoped to one project path). Returns
    (status, job_id) where status is 'cancelled' | 'none' | 'error'."""
    jobs = _running_jobs()
    if project_path:
        jobs = [j for j in jobs if j.get("project") == project_path]
    if not jobs:
        return ("none", None)
    job = jobs[-1]
    jid = job["id"]
    stopped = False
    unit = job.get("scope_unit")
    if unit and shutil.which("systemctl"):
        try:
            r = subprocess.run(["systemctl", "--user", "stop", unit],
                               capture_output=True, text=True, timeout=15)
            stopped = r.returncode == 0
        except Exception:
            stopped = False
    if not stopped and job.get("runner_pid"):
        try:
            os.killpg(os.getpgid(int(job["runner_pid"])), signal.SIGTERM)
            stopped = True
        except Exception:
            try:
                os.kill(int(job["runner_pid"]), signal.SIGTERM)
                stopped = True
            except Exception:
                stopped = False
    store.update_job(jid, status="failed", finished_ts=time.time(),
                     exit_code=130, error="cancelled by user")
    return ("cancelled" if stopped else "error", jid)


def discard_active() -> tuple[str, str]:
    """Revert uncommitted changes in the ACTIVE project's git tree. Returns (status, name) where status
    is 'discarded' | 'no-active' | 'not-git' | 'clean' | 'error'."""
    proj = projects.active()
    if proj is None:
        return ("no-active", "")
    path = proj["path"]
    name = proj["name"]

    def _git(*args: str):
        return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=30)

    try:
        inside = _git("rev-parse", "--is-inside-work-tree")
        if inside.stdout.strip() != "true":
            return ("not-git", name)
        if not _git("status", "--porcelain").stdout.strip():
            return ("clean", name)
        _git("reset", "--hard", "HEAD")
        _git("clean", "-fd")
        return ("discarded", name)
    except Exception:
        return ("error", name)
