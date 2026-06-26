"""Builder project registry + active-pointer."""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    import gabagent.builder.projects as p
    importlib.reload(p)
    return p


def test_slugify():
    from gabagent.builder.projects import slugify
    assert slugify("Snake Game!") == "snake-game"
    assert slugify("  multi   word  ") == "multi-word"
    assert slugify("") == "project"
    assert slugify("", fallback="x") == "x"
    assert len(slugify("a" * 100)) <= 40


def test_register_active_and_set(projects):
    assert projects.active() is None
    projects.register("snake", "/tmp/snake", description="a snake game")
    act = projects.active()
    assert act["name"] == "snake" and act["description"] == "a snake game"
    projects.register("tetris", "/tmp/tetris", make_active=True)
    assert projects.active()["name"] == "tetris"
    assert projects.set_active("snake") is True
    assert projects.active()["name"] == "snake"
    assert projects.set_active("nope") is False


def test_register_preserves_description(projects):
    projects.register("snake", "/tmp/snake", description="original")
    projects.register("snake", "/tmp/snake")  # no description → keep the original
    assert projects.get("snake")["description"] == "original"


def test_new_sandbox_project_creates_dir(projects, tmp_path):
    root = str(tmp_path / "builder")
    rec = projects.new_sandbox_project("Snake Game", root, description="a snake game")
    assert rec["name"] == "snake-game"
    assert (tmp_path / "builder" / "snake-game").is_dir()
    assert projects.active()["name"] == "snake-game"
    with pytest.raises(ValueError):
        projects.new_sandbox_project("x", "")  # no scratch root


def test_list_all_merges_disk_dirs(projects, tmp_path):
    root = tmp_path / "builder"
    (root / "ondisk").mkdir(parents=True)
    projects.register("registered", str(tmp_path / "elsewhere"))
    names = {p["name"] for p in projects.list_all(str(root))}
    assert {"registered", "ondisk"} <= names


def test_resolve(projects, tmp_path):
    root = str(tmp_path / "builder")
    projects.new_sandbox_project("snake-game", root)
    projects.new_sandbox_project("tetris", root)
    status, payload = projects.resolve("tetris", root)  # exact
    assert status == "ok" and payload["name"] == "tetris"
    status, payload = projects.resolve("snake", root)   # unique substring
    assert status == "ok" and payload["name"] == "snake-game"
    status, payload = projects.resolve("nothere", root)
    assert status == "none" and len(payload) == 2
    projects.new_sandbox_project("snake-classic", root)
    status, payload = projects.resolve("snake", root)   # now ambiguous
    assert status == "ambiguous" and len(payload) == 2


def test_effective_target_path(projects, tmp_path):
    from types import SimpleNamespace
    scratch = tmp_path / "builder"
    cfg = SimpleNamespace(builder_scratch_root=str(scratch))
    cwd = tmp_path / "cwd"
    etp = projects.effective_target_path
    # explicit project wins
    assert etp("/abs/proj", None, cwd, cfg) == Path("/abs/proj")
    # spoken project assembled
    assert etp("slash abs slash proj", None, cwd, cfg) == Path("/abs/proj")
    # named → sandbox child
    assert etp(None, "Snake Game", cwd, cfg) == (scratch / "snake-game").resolve()
    # active project (the bug case: omitted project must NOT fall back to cwd)
    projects.new_sandbox_project("snake", str(scratch))
    assert etp(None, None, cwd, cfg) == (scratch / "snake").resolve()
    # no active, scratch set → the sandbox root (a child will live under it)
    projects._mutate(lambda d: d.update({"active": None}))
    assert etp(None, None, cwd, cfg) == scratch.resolve()
    # no scratch configured → cwd (legacy)
    assert etp(None, None, cwd, SimpleNamespace(builder_scratch_root="")) == cwd.resolve()


def test_tier_uses_active_project_not_cwd(projects, tmp_path):
    """Regression: an omitted `project` with an active SANDBOX project must be Tier-1/auto, not keyboard
    (the 'made me do a mouse confirm' bug — tier fell back to the brain's cwd)."""
    from types import SimpleNamespace
    from gabagent.permissions.tiers import tier_of
    scratch = tmp_path / "builder"
    cfg = SimpleNamespace(builder_scratch_root=str(scratch),
                          builder_allowed_roots=[str(scratch)], builder_graduate_root="")
    cwd = tmp_path / "dev" / "voice-agent"   # the brain's real cwd — NOT allow-listed
    projects.new_sandbox_project("snake", str(scratch))   # active = snake, under the sandbox
    assert tier_of("send_to_builder", {"task": "build a game"}, cwd, cfg) == 1
    # an explicit project outside the allow-list is still keyboard-gated
    assert tier_of("send_to_builder", {"task": "x", "project": str(cwd)}, cwd, cfg) == 3


def test_rename_path_graduation(projects, tmp_path):
    root = str(tmp_path / "builder")
    projects.new_sandbox_project("snake", root)
    projects.rename_path("snake", "snake", "/home/u/dev/snake", graduated=True)
    rec = projects.get("snake")
    assert rec["path"] == "/home/u/dev/snake" and rec["graduated"] is True
    assert projects.active()["name"] == "snake"
