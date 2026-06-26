"""Builder ops: cancel a running build, discard uncommitted scratch changes."""
import importlib
import subprocess

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    import gabagent.builder.projects as p
    import gabagent.builder.store as s
    import gabagent.builder.ops as o
    importlib.reload(p)
    importlib.reload(s)
    importlib.reload(o)
    return p, s, o, tmp_path


def test_cancel_none_when_idle(env):
    _, _, o, _ = env
    assert o.cancel_running() == ("none", None)


def test_cancel_marks_job_failed(env, monkeypatch):
    p, s, o, _ = env
    job = s.new_job("build", "/proj")
    s.update_job(job["id"], status="running", scope_unit="gabagent-builder-x.scope")
    # no real systemctl/pid → can't confirm the stop, but the job is finalized as cancelled.
    monkeypatch.setattr("gabagent.builder.ops.shutil.which", lambda _: None)
    status, jid = o.cancel_running()
    assert jid == job["id"]
    assert s.load_job(job["id"])["status"] == "failed"
    assert s.load_job(job["id"])["error"] == "cancelled by user"


def test_discard_no_active(env):
    _, _, o, _ = env
    assert o.discard_active() == ("no-active", "")


def test_discard_reverts_dirty_tree(env, tmp_path):
    p, _, o, _ = env
    proj = tmp_path / "builder" / "snake"
    proj.mkdir(parents=True)
    subprocess.run(["git", "-C", str(proj), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.email", "t@e.st"], check=True)
    subprocess.run(["git", "-C", str(proj), "config", "user.name", "t"], check=True)
    (proj / "a.txt").write_text("committed")
    subprocess.run(["git", "-C", str(proj), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-qm", "base"], check=True)
    p.register("snake", str(proj))

    assert o.discard_active() == ("clean", "snake")
    (proj / "a.txt").write_text("changed")          # dirty the tree
    (proj / "untracked.txt").write_text("new")
    assert o.discard_active() == ("discarded", "snake")
    assert (proj / "a.txt").read_text() == "committed"
    assert not (proj / "untracked.txt").exists()


def test_discard_not_git(env, tmp_path):
    p, _, o, _ = env
    proj = tmp_path / "builder" / "plain"
    proj.mkdir(parents=True)
    p.register("plain", str(proj))
    assert o.discard_active() == ("not-git", "plain")
