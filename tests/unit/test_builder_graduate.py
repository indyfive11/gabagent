"""Builder graduation: name suggestion + the move/allow-list/persist flow."""
import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    import gabagent.builder.projects as p
    importlib.reload(p)
    return p, tmp_path


def test_suggest_name_from_package_json(env, tmp_path):
    from gabagent.builder.graduate import suggest_name
    proj = tmp_path / "raw"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "Cool App"}')
    assert suggest_name(str(proj)) == "cool-app"


def test_suggest_name_from_readme_then_dirname(env, tmp_path):
    from gabagent.builder.graduate import suggest_name
    proj = tmp_path / "rawdir"
    proj.mkdir()
    (proj / "README.md").write_text("# Snake Game\n\nplay it")
    assert suggest_name(str(proj)) == "snake-game"
    bare = tmp_path / "just-a-dir"
    bare.mkdir()
    assert suggest_name(str(bare)) == "just-a-dir"


def test_graduate_moves_allowlists_and_persists(env, tmp_path):
    from gabagent.config.models import GabAgentConfig
    from gabagent.config.loader import save_config, load_config
    p, _ = env
    scratch = tmp_path / "builder"
    grad = tmp_path / "dev"
    cfg = GabAgentConfig(api_key="test", builder_scratch_root=str(scratch),
                         builder_graduate_root=str(grad), builder_allowed_roots=[str(scratch)])
    ctx = SimpleNamespace(config=cfg, cwd=tmp_path)

    p.new_sandbox_project("snake", str(scratch))
    (scratch / "snake" / "main.py").write_text("print('hi')")

    from gabagent.builder.graduate import graduate
    final, dest = graduate(ctx, name="snake")
    assert final == "snake"
    assert (grad / "snake" / "main.py").is_file()        # moved
    assert not (scratch / "snake").exists()              # source gone
    assert str(grad / "snake") in cfg.builder_allowed_roots  # allow-listed in memory
    # persisted to settings.json
    assert str(grad / "snake") in load_config().builder_allowed_roots
    # registry re-pointed
    assert p.get("snake")["graduated"] is True and p.get("snake")["path"] == str(grad / "snake")


def test_graduate_errors(env, tmp_path):
    from gabagent.builder.graduate import graduate
    p, _ = env
    scratch = tmp_path / "builder"
    # no active project
    ctx = SimpleNamespace(config=SimpleNamespace(
        builder_graduate_root=str(tmp_path / "dev"), builder_allowed_roots=[]), cwd=tmp_path)
    with pytest.raises(ValueError, match="no current builder project"):
        graduate(ctx)
    # no graduate root configured
    p.new_sandbox_project("snake", str(scratch))
    ctx.config.builder_graduate_root = ""
    with pytest.raises(ValueError, match="graduation folder"):
        graduate(ctx, name="snake")
