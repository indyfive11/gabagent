import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gabagent.agent.context import AgentContext


# ----- store -----------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    import importlib
    import gabagent.builder.store as s
    importlib.reload(s)
    return s


def test_store_roundtrip_and_perms(store):
    job = store.new_job("do a thing", "/proj", model=None)
    assert job["status"] == "queued"
    loaded = store.load_job(job["id"])
    assert loaded["task"] == "do a thing"
    # 0600 — task/summary can carry sensitive content
    mode = os.stat(store._job_path(job["id"])).st_mode & 0o777
    assert mode == 0o600


def test_store_update_and_list(store):
    a = store.new_job("a", "/p")
    b = store.new_job("b", "/p")
    store.update_job(a["id"], status="running")
    assert store.load_job(a["id"])["status"] == "running"
    ids = {j["id"] for j in store.list_jobs()}
    assert ids == {a["id"], b["id"]}


def test_undelivered_done(store):
    a = store.new_job("a", "/p")
    store.update_job(a["id"], status="done")
    assert [j["id"] for j in store.undelivered_done()] == [a["id"]]
    store.update_job(a["id"], delivered=True)
    assert store.undelivered_done() == []


def test_update_missing_job_returns_none(store):
    assert store.update_job("nope-000000", status="done") is None


# ----- runner ----------------------------------------------------------------

def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.st"], check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_identity_env_reads_user_config(store, tmp_path):
    import gabagent.builder.runner as runner
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_repo(proj)
    env = runner._identity_env(str(proj))
    assert env["GIT_AUTHOR_NAME"] == "Tester"
    assert env["GIT_COMMITTER_EMAIL"] == "t@e.st"


def test_identity_env_empty_when_unconfigured(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    # With global+system config emptied and no local repo, git resolves no identity → inject nothing.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    assert runner._identity_env(str(tmp_path)) == {}


def test_build_cmd_denies_push_and_bypasses(store):
    import gabagent.builder.runner as runner
    cmd = runner._build_cmd("fix it", "/proj", model="claude-opus-4-8")
    assert "--permission-mode" in cmd and "bypassPermissions" in cmd
    assert "Bash(git push:*)" in cmd and "Bash(gh:*)" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"


def test_runner_ground_truth_success(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_repo(proj)
    job = store.new_job("add a file", str(proj))

    # Capture the REAL run before patching — patching runner.subprocess.run swaps it on the shared
    # module object, so a fallback to subprocess.run would otherwise recurse into the fake.
    real_run = subprocess.run

    # Fake the claude build: create + commit a file, print a (partly unverifiable) self-report.
    def fake_run(cmd, *a, **kw):
        if cmd[0] == "claude":
            (Path(kw["cwd"]) / "new.py").write_text("x = 1\n")
            real_run(["git", "-C", kw["cwd"], "add", "-A"], check=True)
            real_run(["git", "-C", kw["cwd"], "commit", "-q", "-m", "add new.py"], check=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="Done — and pushed it!", stderr="")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    rc = runner.run(job["id"])
    assert rc == 0
    done = store.load_job(job["id"])
    assert done["status"] == "done"
    assert done["exit_code"] == 0
    assert len(done["commits"]) == 1
    assert "new.py" in done["files_changed"]
    # ground truth in the summary; self-report kept but labelled unverified
    assert "Commits: 1 new" in done["summary"]
    assert "unverified" in done["summary"]


def test_runner_nonzero_exit_marks_failed(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_repo(proj)
    job = store.new_job("break", str(proj))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "claude":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.run(job["id"]) == 1
    failed = store.load_job(job["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"


def test_runner_timeout_marks_failed(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_repo(proj)
    job = store.new_job("slow", str(proj))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "claude":
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 1)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.run(job["id"]) == 1
    failed = store.load_job(job["id"])
    assert failed["status"] == "failed"
    assert failed["exit_code"] == 124


def test_runner_rejects_non_git_dir(store, tmp_path):
    import gabagent.builder.runner as runner
    proj = tmp_path / "plain"
    proj.mkdir()
    job = store.new_job("x", str(proj))
    assert runner.run(job["id"]) == 1
    assert store.load_job(job["id"])["status"] == "failed"


def test_spoken_summary_is_ground_truth_terse():
    import gabagent.builder.runner as runner
    done = {"project": "/home/rob/dev/voice-agent", "status": "done",
            "commits": ["abc x"], "files_changed": ["a.py", "b.py"]}
    line = runner._spoken_summary(done)
    assert "voice-agent" in line and "1 commit" in line and "2 files" in line
    failed = {"project": "/p/gab", "status": "failed", "error": "boom\nmore"}
    fl = runner._spoken_summary(failed)
    assert "failed" in fl and "gab" in fl and "boom" in fl and "more" not in fl


def test_runner_success_announces_onto_channel(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    from gabagent.voice import announce_store
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_repo(proj)
    job = store.new_job("add a file", str(proj))
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "claude":
            (Path(kw["cwd"]) / "n.py").write_text("x = 1\n")
            real_run(["git", "-C", kw["cwd"], "add", "-A"], check=True)
            real_run(["git", "-C", kw["cwd"], "commit", "-q", "-m", "add n.py"], check=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.run(job["id"])
    # The finished build is on the deferred-announce channel, keyed by job_id, free-for-all (no session).
    items = announce_store.pending()
    assert [i["job_id"] for i in items] == [job["id"]]
    assert "Builder job done" in items[0]["text"]
    assert items[0]["preferred_session"] is None


# ----- tools -----------------------------------------------------------------

def _ctx(tmp_path, scratch_root="", graduate_root=""):
    from types import SimpleNamespace
    ctx = MagicMock(spec=AgentContext)
    ctx.cwd = tmp_path
    ctx.headless = True
    ctx.config = SimpleNamespace(
        builder_scratch_root=scratch_root,
        builder_graduate_root=graduate_root,
        builder_allowed_roots=[],
    )
    return ctx


@pytest.mark.asyncio
async def test_send_to_builder_dispatches(store, tmp_path, monkeypatch):
    from gabagent.tools.builder_tool import SendToBuilderTool
    proj = tmp_path / "proj"
    proj.mkdir()
    _init_repo(proj)
    spawned = {}
    monkeypatch.setattr("gabagent.tools.builder_tool._spawn_runner",
                        lambda jid: spawned.setdefault("id", jid))
    res = await SendToBuilderTool().execute(_ctx(proj), task="do it", project=str(proj))
    assert res.success
    assert spawned["id"] in res.output
    assert store.load_job(spawned["id"])["task"] == "do it"


@pytest.mark.asyncio
async def test_send_to_builder_asks_when_non_git(store, tmp_path, monkeypatch):
    # A non-git target is NOT a hard error (no handholding) — the builder ASKS init vs scratch and does
    # NOT dispatch until the caller chooses.
    from gabagent.tools.builder_tool import SendToBuilderTool
    plain = tmp_path / "plain"
    plain.mkdir()
    spawned = {}
    monkeypatch.setattr("gabagent.tools.builder_tool._spawn_runner",
                        lambda jid: spawned.setdefault("id", jid))
    res = await SendToBuilderTool().execute(_ctx(plain), task="x", project=str(plain))
    assert res.success                                  # asks, not errors
    assert "isn't a git repo" in res.output
    assert "init" in res.output and "scratch" in res.output
    assert "id" not in spawned                          # did NOT dispatch
    assert store.list_jobs() == []                      # no job created


@pytest.mark.asyncio
async def test_send_to_builder_scratch_and_init_dispatch(store, tmp_path, monkeypatch):
    from gabagent.tools.builder_tool import SendToBuilderTool
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr("gabagent.tools.builder_tool._spawn_runner", lambda jid: None)
    sc = await SendToBuilderTool().execute(_ctx(plain), task="x", project=str(plain), git_mode="scratch")
    assert sc.success and "scratch" in sc.output.lower()
    init = await SendToBuilderTool().execute(_ctx(plain), task="y", project=str(plain), git_mode="init")
    assert init.success and "git repo" in init.output
    modes = {j["git_mode"] for j in store.list_jobs()}
    assert modes == {"scratch", "init"}


def test_runner_init_mode_autoinits_and_builds(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    # identity for the auto-init base + build commits (a fresh repo has no local config)
    for k, v in {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.st",
                 "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.st"}.items():
        monkeypatch.setenv(k, v)
    proj = tmp_path / "fresh"
    proj.mkdir()  # NOT a git repo
    job = store.new_job("add a file", str(proj), git_mode="init")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            (Path(kw["cwd"]) / "made.py").write_text("y = 2\n")
            real_run(["git", "-C", kw["cwd"], "add", "-A"], check=True)
            real_run(["git", "-C", kw["cwd"], "commit", "-q", "-m", "add made.py"], check=True, env=kw.get("env"))
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.run(job["id"]) == 0
    done = store.load_job(job["id"])
    assert done["status"] == "done"
    assert (proj / ".git").is_dir()            # auto-init created the repo
    assert "made.py" in done["files_changed"]
    assert len(done["commits"]) == 1           # build commit on top of the auto-init base


def test_runner_scratch_mode_gitless_snapshot(store, tmp_path, monkeypatch):
    import gabagent.builder.runner as runner
    proj = tmp_path / "scratch"
    proj.mkdir()
    (proj / "existing.txt").write_text("old\n")
    job = store.new_job("make a file", str(proj), git_mode="scratch")
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd and cmd[0] == "claude":
            (Path(kw["cwd"]) / "newfile.txt").write_text("fresh\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="made it", stderr="")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.run(job["id"]) == 0
    done = store.load_job(job["id"])
    assert done["status"] == "done"
    assert done["commits"] == []
    assert "newfile.txt" in done["files_changed"]   # snapshot delta caught the new file
    assert not (proj / ".git").exists()             # gitless — no repo created
    assert "Scratch" in done["summary"]


def test_spoken_summary_scratch_and_uncommitted():
    import gabagent.builder.runner as runner
    scratch = {"project": "/p/scr", "status": "done", "git_mode": "scratch",
               "commits": [], "files_changed": ["a", "b"]}
    s = runner._spoken_summary(scratch)
    assert "Scratch" in s and "2 files" in s and "push" not in s
    # the phrasing-nit fix: 0 commits but uncommitted changes → "review", not "push it yourself"
    uncommitted = {"project": "/p/gab", "status": "done", "git_mode": "repo",
                   "commits": [], "files_changed": ["x"]}
    u = runner._spoken_summary(uncommitted)
    assert "uncommitted" in u and "push" not in u


@pytest.mark.asyncio
async def test_check_builder_surfaces_and_marks_delivered(store, tmp_path):
    from gabagent.tools.builder_tool import CheckBuilderTool
    job = store.new_job("a", "/p")
    store.update_job(job["id"], status="done", summary="Builder job X — DONE.")
    res = await CheckBuilderTool().execute(_ctx(tmp_path))
    assert "DONE" in res.output
    assert store.load_job(job["id"])["delivered"] is True
    # second call: nothing new
    res2 = await CheckBuilderTool().execute(_ctx(tmp_path))
    assert "No new results" in res2.output
