"""Builder dispatch tools (phase 0, text-first): send_to_builder + check_builder.

`send_to_builder` queues a coding task and spawns a DETACHED runner (its own systemd scope, so it
survives the brain's teardown). `check_builder` surfaces ground-truth results. No voice / no proactive
channel here — that's phases 2-3; phase 0 proves the whole dispatch loop on text alone.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, TYPE_CHECKING

from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def _spawn_runner(job_id: str) -> None:
    """Launch the builder runner DETACHED, in its own systemd scope when available, so a brain/voice
    cgroup cycle on teardown doesn't reap a long-running build (mirrors cli._spawn_persona_reflect)."""
    argv = [sys.executable, "-m", "gabagent.builder.runner", job_id]
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        try:
            subprocess.Popen(
                [systemd_run, "--user", "--scope", "--quiet", "--collect", "--", *argv],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            return
        except Exception:
            pass  # systemd-run present but unusable → fall back
    subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


def _is_git_repo(project: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() == "true"
    except Exception:
        return False


@registry.register
class SendToBuilderTool(ToolBase):
    name = "send_to_builder"
    description = (
        "Dispatch a self-contained CODING task to a detached headless builder (Claude Code) that works "
        "in a chosen project and reports back a verified result. Use for 'go build/fix/refactor X in "
        "project Y' — the builder writes, runs, and commits LOCALLY but does NOT push (it leaves a "
        "reviewable diff). It runs in the background; the result is surfaced later via check_builder. "
        "Distinct from send_to_claude, which just messages Rob's interactive Claude Code session."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The coding task, self-contained and specific (the builder has no chat context).",
            },
            "project": {
                "type": "string",
                "description": "Absolute path to the target project (a git work tree). Defaults to the current directory.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override for the builder (e.g. 'claude-opus-4-8').",
            },
            "git_mode": {
                "type": "string",
                "enum": ["init", "scratch"],
                "description": (
                    "ONLY needed when the target is NOT already a git repo. 'init' = create a git repo "
                    "there first (keeps a reviewable diff + verified ground-truth summary; recommended). "
                    "'scratch' = run throwaway with no git (no diff/commit, lighter file-snapshot summary). "
                    "OMIT this to be ASKED which you want — the builder will not silently pick. Ignored "
                    "when the target already is a git repo."
                ),
            },
        },
        "required": ["task"],
    }

    async def execute(
        self, ctx: AgentContext, task: str, project: str | None = None,
        model: str | None = None, git_mode: str | None = None, **kwargs: Any,
    ) -> ToolResult:
        try:
            target = Path(project).expanduser() if project else Path(ctx.cwd)
            target = target.resolve()
            if not target.is_dir():
                return ToolResult(output="", error=f"Project path is not a directory: {target}")

            # A git repo is the DEFAULT (it's what makes the result a reviewable diff + a verified
            # ground-truth summary), but not a hard wall: for a non-git target, ASK rather than refuse —
            # the user may want a throwaway scratch build. No silent auto-pick.
            if _is_git_repo(target):
                mode = "repo"
            elif git_mode in ("init", "scratch"):
                mode = git_mode
            else:
                return ToolResult(output=(
                    f"{target} isn't a git repo. A git repo gives a reviewable diff and a verified "
                    f"(ground-truth) summary. How do you want to run this build?\n"
                    f"  • init — create a git repo there first (recommended; keeps the diff + verified summary)\n"
                    f"  • scratch — run it throwaway with no git (no diff/commit, lighter summary)\n"
                    f"Re-dispatch the same task with git_mode set to 'init' or 'scratch' — or skip it."
                ))

            from gabagent.builder import store
            job = store.new_job(task=task, project=str(target), model=model, git_mode=mode)
            _spawn_runner(job["id"])
            note = {"repo": "reviewable-diff, no push",
                    "init": "fresh git repo, reviewable-diff",
                    "scratch": "scratch — no git"}[mode]
            return ToolResult(output=(
                f"Builder job {job['id']} dispatched in {target} ({note}). "
                f"It runs detached. Use check_builder to see the result."
            ))
        except Exception as e:
            return ToolResult(output="", error=str(e))


@registry.register
class CheckBuilderTool(ToolBase):
    name = "check_builder"
    description = (
        "Check on dispatched builder jobs. With no arguments: lists in-flight jobs and surfaces any "
        "newly-finished results (marking them seen). With a job_id: returns that job's full ground-truth "
        "summary."
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "A specific job id to fetch in full."},
        },
        "required": [],
    }

    async def execute(self, ctx: AgentContext, job_id: str | None = None, **kwargs: Any) -> ToolResult:
        try:
            from gabagent.builder import store
            if job_id:
                job = store.load_job(job_id)
                if job is None:
                    return ToolResult(output=f"No builder job {job_id}.")
                return ToolResult(output=job.get("summary") or f"Job {job_id}: {job.get('status')}")

            jobs = store.list_jobs()
            if not jobs:
                return ToolResult(output="No builder jobs.")
            parts = []
            done = [j for j in jobs if j.get("status") in ("done", "failed") and not j.get("delivered")]
            for j in done:
                parts.append(j.get("summary") or f"Job {j['id']}: {j['status']}")
                store.update_job(j["id"], delivered=True)
            active = [j for j in jobs if j.get("status") in ("queued", "running")]
            if active:
                parts.append("In flight:\n" + "\n".join(
                    f"  {j['id']} — {j['status']} — {j['task'][:60]}" for j in active
                ))
            if not parts:
                parts.append("No new results; nothing in flight.")
            return ToolResult(output="\n\n".join(parts))
        except Exception as e:
            return ToolResult(output="", error=str(e))
