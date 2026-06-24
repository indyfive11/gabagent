"""Detached builder runner — executes ONE dispatched coding task in a headless `claude -p`.

Spawned as `python -m gabagent.builder.runner <job_id>` inside its own `systemd-run --user --scope`
(see tools/builder_tool._spawn_runner) so it OUTLIVES the brain/voice cgroup that dispatched it — the
same reaping hazard persona.reflect_detached documents (a plain child is killed when the brain's
cgroup cycles on teardown). Self-contained: reads the job from the flock'd store, runs the build, and
writes a GROUND-TRUTH summary back.

Ground truth, not self-report (anti-confab): the surfaced summary leads with VERIFIED facts — the
process exit code and the git delta (new commits, files touched) — because `claude -p` returns the
model's final text, and a model that hit a non-fast-forward but believes it pushed will SAY it pushed.

Phase 0 / M5 = reviewable-diff: the builder has full LOCAL authority (write / run / commit) but MUST
NOT push — `git push`/`gh` are hard-denied and the runner never pushes. Git author+committer are pinned
to the user's OWN configured identity (top of git's precedence) so the builder's commits can't drift to
gecos/ambient config — read from the user's git config, never hardcoded, so the public repo stays clean.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from gabagent.builder import store

_BUILD_TIMEOUT = 1800  # 30 min — a coding task is long by nature; on timeout the job is marked failed


def _git(project: str, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", project, *args],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _is_git_repo(project: str) -> bool:
    return _git(project, "rev-parse", "--is-inside-work-tree") == "true"


def _identity_env(project: str) -> dict[str, str]:
    """Pin git author+committer to the user's OWN configured identity (repo→global precedence) at the
    TOP of git's lookup order. The builder commits with no human reviewing each commit, so we force the
    identity rather than trust gecos/ambient config (git identity is NOT inherited from CLAUDE.md or any
    agent prompt). Read from the user's config — never a hardcoded name — so the public repo carries no
    identity. Unset config → inject nothing (safe default = unchanged behavior)."""
    name = _git(project, "config", "user.name")
    email = _git(project, "config", "user.email")
    if name and email:
        return {
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
        }
    return {}


def _build_cmd(task: str, project: str, model: str | None) -> list[str]:
    cmd = [
        "claude", "-p", task,
        "--add-dir", project,
        "--permission-mode", "bypassPermissions",  # full build authority (Rob: "I am the security")
        # Phase-0 reviewable-diff: hard-deny push so the result stays a local diff Rob reviews.
        "--disallowedTools", "Bash(git push:*)", "Bash(gh:*)",
        "--output-format", "text",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _delta(project: str, base: str) -> tuple[list[str], list[str], bool]:
    """Ground-truth git delta since `base`: (new commits, files changed, has-uncommitted)."""
    commits, files = [], []
    if base:
        commits = [l for l in _git(project, "log", "--oneline", f"{base}..HEAD").splitlines() if l.strip()]
        files = [l for l in _git(project, "diff", "--name-only", base, "HEAD").splitlines() if l.strip()]
    porcelain = _git(project, "status", "--porcelain")
    for line in porcelain.splitlines():
        fn = line[3:].strip()
        if fn and fn not in files:
            files.append(fn)
    return commits, files, bool(porcelain.strip())


def _summarize(job: dict) -> str:
    lines = [
        f"Builder job {job['id']} — {job['status'].upper()}.",
        f"Project: {job['project']}",
        f"Exit code: {job['exit_code']}",
    ]
    bb, fb = job.get("base_branch"), job.get("final_branch")
    if bb or fb:
        lines.append(f"Branch: {bb} → {fb}" if bb != fb else f"Branch: {fb}")
    commits = job.get("commits") or []
    lines.append(f"Commits: {len(commits)} new" + (f" ({'; '.join(commits[:5])}{', …' if len(commits) > 5 else ''})" if commits else ""))
    files = job.get("files_changed") or []
    lines.append(f"Files changed: {len(files)}" + (f" ({', '.join(files[:10])}{', …' if len(files) > 10 else ''})" if files else ""))
    lines.append(f"Uncommitted changes: {'yes' if job.get('uncommitted') else 'no'}")
    if job.get("error"):
        lines.append(f"Error: {job['error']}")
    report = (job.get("self_report") or "").strip()
    if report:
        clipped = report if len(report) <= 1200 else report[:1200] + " …[truncated]"
        lines.append("\nBuilder's own report (unverified):\n" + clipped)
    if job.get("git_mode") == "scratch":
        if files:
            lines.append("\nScratch build (no git) — changes are in the working tree; inspect them.")
    elif commits:
        lines.append("\nReview the diff and push it yourself when ready (phase 0 does not push).")
    elif files:
        lines.append("\nChanges are uncommitted in the working tree — review them (nothing committed to push).")
    return "\n".join(lines)


def _spoken_summary(job: dict) -> str:
    """A terse, GROUND-TRUTH one-liner for TTS — derived from verified facts (exit code + git delta), not
    the model's self-report. The full multi-line summary stays in the store for the TUI / check_builder."""
    proj = os.path.basename(job.get("project", "").rstrip("/")) or "the project"
    if job.get("status") != "done":
        err = (job.get("error") or "").strip().splitlines()[0:1]
        tail = f" — {err[0]}" if err else ""
        return f"Builder job failed in {proj}{tail}. Check the terminal."
    nc = len(job.get("commits") or [])
    nf = len(job.get("files_changed") or [])
    if job.get("git_mode") == "scratch":
        return (f"Scratch builder job done in {proj}: {nf} file{'s' if nf != 1 else ''} changed. "
                f"No git — inspect the working tree.")
    tail = ("Review the diff and push it yourself." if nc
            else "The changes are uncommitted — review them." if nf
            else "No changes were made.")
    return (
        f"Builder job done in {proj}: {nc} commit{'s' if nc != 1 else ''}, "
        f"{nf} file{'s' if nf != 1 else ''} changed. {tail}"
    )


def _announce_result(job: dict) -> None:
    """Push the finished build onto the deferred-announce channel so the voice side can speak it (phase 2).
    No owning voice session (the runner is detached + device-free) → preferred_session=None ⇒ any present
    device may speak it. Best-effort: a store/import hiccup must not fail the build."""
    try:
        from gabagent.voice import announce_store
        announce_store.enqueue(_spoken_summary(job), job_id=job["id"], source="builder")
    except Exception:
        pass


def _execute_build(job_id: str, job: dict, project: str, env: dict) -> tuple | None:
    """Run the `claude -p` build subprocess. On timeout/exception, finalize the job as failed + announce,
    and return None. On completion, return (exit_code, self_report, stderr_or_None)."""
    cmd = _build_cmd(job["task"], project, job.get("model"))
    try:
        proc = subprocess.run(cmd, cwd=project, capture_output=True, text=True,
                              env=env, timeout=_BUILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        j = store.update_job(job_id, status="failed", finished_ts=time.time(), exit_code=124,
                             error=f"builder timed out after {_BUILD_TIMEOUT}s")
        _announce_result(j or store.load_job(job_id))
        return None
    except Exception as e:
        j = store.update_job(job_id, status="failed", finished_ts=time.time(), exit_code=1, error=str(e))
        _announce_result(j or store.load_job(job_id))
        return None
    code = proc.returncode
    self_report = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()[:2000] or None if code != 0 else None
    return code, self_report, err


def _init_repo_with_base(project: str, env: dict) -> bool:
    """git_mode='init': make a non-git target a git repo with a base commit, so the build still yields a
    reviewable diff + git-delta ground truth. The base commit's identity comes from the user's config
    (env already carries _identity_env); falls back to a NEUTRAL 'builder' marker if none is set — never a
    hardcoded user identity (public-repo safety)."""
    _git(project, "init")
    _git(project, "add", "-A")
    benv = dict(env)
    benv.setdefault("GIT_AUTHOR_NAME", "builder")
    benv.setdefault("GIT_AUTHOR_EMAIL", "builder@localhost")
    benv.setdefault("GIT_COMMITTER_NAME", "builder")
    benv.setdefault("GIT_COMMITTER_EMAIL", "builder@localhost")
    try:
        subprocess.run(["git", "-C", project, "commit", "--allow-empty", "-m", "builder: base (auto-init)"],
                       capture_output=True, text=True, timeout=30, env=benv)
    except Exception:
        pass
    return _is_git_repo(project)


def _snapshot(project: str) -> dict:
    """Map of file → (mtime, size) for the scratch-mode before/after delta. Skips .git."""
    snap: dict[str, tuple] = {}
    root = Path(project)
    for p in root.rglob("*"):
        sp = str(p)
        if "/.git/" in sp or sp.endswith("/.git"):
            continue
        if p.is_file():
            try:
                st = p.stat()
                snap[str(p.relative_to(root))] = (st.st_mtime, st.st_size)
            except OSError:
                pass
    return snap


def _run_scratch(job_id: str, job: dict, project: str) -> int:
    """git_mode='scratch': gitless throwaway build — no commit, no diff. Ground truth stays honest via a
    before/after file-snapshot delta (created/modified files), NOT the model's self-report."""
    before = _snapshot(project)
    store.update_job(job_id, status="running", started_ts=time.time())
    result = _execute_build(job_id, job, project, dict(os.environ))  # no identity pin — nothing commits
    if result is None:
        return 1
    code, self_report, err = result
    after = _snapshot(project)
    files = sorted(f for f, meta in after.items() if before.get(f) != meta)
    status = "done" if code == 0 else "failed"
    updated = store.update_job(
        job_id, status=status, finished_ts=time.time(), exit_code=code,
        commits=[], files_changed=files, uncommitted=bool(files), self_report=self_report, error=err,
    ) or store.load_job(job_id)
    final = store.update_job(job_id, summary=_summarize(updated)) or updated
    _announce_result(final)
    return 0 if code == 0 else 1


def run(job_id: str) -> int:
    job = store.load_job(job_id)
    if job is None:
        return 2
    project = job["project"]
    mode = job.get("git_mode", "repo")

    if mode == "scratch":
        return _run_scratch(job_id, job, project)

    env = {**os.environ, **_identity_env(project)}
    # repo (default) or init: a git work tree with a base commit, so the build is a reviewable diff.
    if not _is_git_repo(project):
        if not (mode == "init" and _init_repo_with_base(project, env)):
            store.update_job(job_id, status="failed", finished_ts=time.time(), exit_code=1,
                             error=f"{project} is not a git work tree")
            return 1

    base = _git(project, "rev-parse", "HEAD")
    base_branch = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
    store.update_job(job_id, status="running", started_ts=time.time(),
                     base_commit=base, base_branch=base_branch)

    result = _execute_build(job_id, job, project, env)
    if result is None:
        return 1
    code, self_report, err = result

    commits, files, uncommitted = _delta(project, base)
    final_branch = _git(project, "rev-parse", "--abbrev-ref", "HEAD")
    status = "done" if code == 0 else "failed"
    updated = store.update_job(
        job_id, status=status, finished_ts=time.time(), exit_code=code,
        final_branch=final_branch, commits=commits, files_changed=files,
        uncommitted=uncommitted, self_report=self_report, error=err,
    ) or store.load_job(job_id)
    final = store.update_job(job_id, summary=_summarize(updated)) or updated
    _announce_result(final)
    return 0 if code == 0 else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return 2
    try:
        return run(argv[0])
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
