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


def builder_scope_unit(job_id: str) -> str:
    """Deterministic transient-scope name so 'cancel the build' can stop the right unit."""
    return f"gabagent-builder-{job_id}.scope"


def _spawn_runner(job_id: str) -> None:
    """Launch the builder runner DETACHED, in its own NAMED systemd scope when available, so a
    brain/voice cgroup cycle on teardown doesn't reap a long-running build (mirrors
    cli._spawn_persona_reflect) and so the scope can be stopped on cancel. The runner pid is recorded
    either way for the no-systemd fallback path."""
    from gabagent.builder import store
    argv = [sys.executable, "-m", "gabagent.builder.runner", job_id]
    systemd_run = shutil.which("systemd-run")
    if systemd_run:
        try:
            unit = builder_scope_unit(job_id)
            proc = subprocess.Popen(
                [systemd_run, "--user", "--scope", "--quiet", "--collect",
                 f"--unit={unit[:-len('.scope')]}", "--", *argv],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
            store.update_job(job_id, scope_unit=unit, runner_pid=proc.pid)
            return
        except Exception:
            pass  # systemd-run present but unusable → fall back
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )
    store.update_job(job_id, runner_pid=proc.pid)


def _is_git_repo(project: Path) -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() == "true"
    except Exception:
        return False


def _in_sandbox(target: Path, scratch_root: str) -> bool:
    if not scratch_root:
        return False
    try:
        return target.is_relative_to(Path(scratch_root).expanduser().resolve())
    except (ValueError, AttributeError):
        return False


@registry.register
class SendToBuilderTool(ToolBase):
    name = "send_to_builder"
    description = (
        "Dispatch a self-contained CODING task to a detached headless builder (Claude Code) that works "
        "in a chosen project and reports back a verified result. Use for 'go build/fix/refactor X in "
        "project Y' — the builder writes, runs, and commits LOCALLY but does NOT push (it leaves a "
        "reviewable diff). It runs in the background; the result is surfaced later via check_builder. "
        "Distinct from send_to_claude, which just messages Rob's interactive Claude Code session. "
        "When no `project`/`name` is given it targets the CURRENT builder project automatically — do NOT "
        "ask the user which project unless they explicitly name a different one."
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
                    "ONLY needed when an EXPLICIT non-git `project` is given. 'init' = create a git repo "
                    "there first (keeps a reviewable diff + verified ground-truth summary; recommended). "
                    "'scratch' = run throwaway with no git. OMIT to be ASKED. Ignored for git repos and "
                    "for sandbox projects (those auto-init)."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Optional project name. With no `project`, dispatch into a NEW sandbox project of "
                    "this name (e.g. the user says 'in a new project called snake'). Omit both `project` "
                    "and `name` to target the CURRENT builder project (or auto-name one from the task)."
                ),
            },
        },
        "required": ["task"],
    }

    async def execute(
        self, ctx: AgentContext, task: str, project: str | None = None,
        model: str | None = None, git_mode: str | None = None, name: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        try:
            from gabagent.builder import projects as projreg
            from gabagent.voice.spoken_tokens import maybe_assemble_path, normalize_name
            # Brain owns spoken→token assembly: a path/name dictated as "slash tmp slash x dash y" or
            # "snake dash game" becomes /tmp/x-y / snake-game. Guarded — a clean value is untouched.
            project = maybe_assemble_path(project) if project else project
            name = normalize_name(name) if name else name
            scratch_root = (getattr(ctx.config, "builder_scratch_root", "") or "").strip()
            desc = (task.strip().splitlines()[0][:80] if task.strip() else "")

            # Resolve the target project: explicit path > named-new-sandbox > current > auto-sandbox > cwd.
            proj_name: str | None = None
            in_sandbox = False
            if project:
                target = Path(project).expanduser().resolve()
                proj_name = projreg.slugify(name) if name else target.name
            elif name and scratch_root:
                rec = projreg.new_sandbox_project(name, scratch_root, description=desc)
                target, proj_name, in_sandbox = Path(rec["path"]).resolve(), rec["name"], True
            elif (act := projreg.active()) is not None:
                target, proj_name = Path(act["path"]).resolve(), act["name"]
                in_sandbox = _in_sandbox(target, scratch_root)
            elif scratch_root:
                rec = projreg.new_sandbox_project(desc or "project", scratch_root, description=desc)
                target, proj_name, in_sandbox = Path(rec["path"]).resolve(), rec["name"], True
            else:
                target = Path(ctx.cwd).resolve()

            if not target.is_dir():
                return ToolResult(output="", error=f"Project path is not a directory: {target}")

            # A git repo is the DEFAULT (it makes the result a reviewable diff + verified ground-truth
            # summary). A sandbox project auto-inits (no ask). Any OTHER non-git target: ASK, don't refuse.
            if _is_git_repo(target):
                mode = "repo"
            elif in_sandbox:
                mode = "init"
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
            if proj_name:  # register + make current so list/switch/graduate see it
                projreg.register(proj_name, str(target), description=desc)
            note = {"repo": "reviewable-diff, no push",
                    "init": "fresh git repo, reviewable-diff",
                    "scratch": "scratch — no git"}[mode]
            where = f"project {proj_name}" if proj_name else str(target)
            return ToolResult(output=(
                f"Builder job {job['id']} dispatched in {where} ({note}). "
                f"It runs detached. Use check_builder to see the result."
            ))
        except Exception as e:
            return ToolResult(output="", error=str(e))


@registry.register
class ManageBuilderTool(ToolBase):
    name = "manage_builder"
    description = (
        "Manage builder PROJECTS and in-flight builds (not for dispatching work — that's send_to_builder). "
        "Actions: 'list' projects; 'switch' the current project (name required); 'new' empty project "
        "(name optional); 'graduate' the current project to its real home (name optional — omit to get a "
        "suggested name to confirm); 'status' of builds; 'working' (what's the current project + build); "
        "'cancel' the running build; 'discard' the current project's uncommitted changes; 'help'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "switch", "new", "graduate", "status", "working", "cancel", "discard", "help"],
                "description": "Which builder management action to perform.",
            },
            "name": {
                "type": "string",
                "description": "Project name — for 'switch' (target), 'new' (optional), 'graduate' (optional new name).",
            },
        },
        "required": ["action"],
    }

    async def execute(self, ctx: AgentContext, action: str, name: str | None = None,
                      **kwargs: Any) -> ToolResult:
        try:
            from gabagent.builder import vui
            return ToolResult(output=vui.respond(ctx, action, (name or "").strip()))
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
            from gabagent.builder import projects as projreg
            act = projreg.active()
            header = f"Current builder project: {act['name']}." if act else None
            if not jobs:
                return ToolResult(output="\n".join(filter(None, [header, "No builder jobs."])))
            parts = [header] if header else []
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
