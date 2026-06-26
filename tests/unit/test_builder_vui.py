"""Builder VUI: deterministic phrase detection + the respond() handlers."""
import importlib
from types import SimpleNamespace

import pytest

from gabagent.builder import vui


def _d(text):
    mc = vui.detect(text)
    return None if mc is None else (mc.value, mc.arg)


@pytest.mark.parametrize("phrase,expected", [
    ("list builder projects", ("list", "")),
    ("what builder projects do I have", ("list", "")),
    ("new builder project called snake", ("new", "snake")),
    ("new builder project", ("new", "")),
    ("switch builder to tetris", ("switch", "tetris")),
    ("builder, work on tetris", ("switch", "tetris")),
    ("open builder project snake game", ("switch", "snake game")),
    ("graduate it", ("graduate", "")),
    ("graduate it as snake-game", ("graduate", "snake-game")),
    ("builder status", ("status", "")),
    ("how's the build", ("status", "")),
    ("how is the build", ("status", "")),
    ("what's the builder working on", ("working", "")),
    ("cancel the build", ("cancel", "")),
    ("stop the builder", ("cancel", "")),
    ("cancel all the builder tasks", ("cancel", "")),       # determiners between verb and noun
    ("cancel that build", ("cancel", "")),
    ("graduated as snake", ("graduate", "snake")),          # past-tense STT of the imperative
    ("graduate as snake", ("graduate", "snake")),           # no "it"
    ("graduate it as snake game", ("graduate", "snake game")),
    ("discard that build", ("discard", "")),
    ("builder help", ("help", "")),
])
def test_detect_maps_phrases(phrase, expected):
    assert _d(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "start a builder task to create hello.txt",   # the DISPATCH verb must stay LLM-routed
    "i canceled the build",                        # past-tense statement, not a command
    "play some jazz",
    "switch to local",
    "stop the music",
    "what time is it",
])
def test_detect_ignores_non_builder(phrase):
    assert vui.detect(phrase) is None


# -- handlers --------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    import gabagent.builder.projects as p
    import gabagent.builder.store as s
    importlib.reload(p)
    importlib.reload(s)
    scratch = tmp_path / "builder"
    grad = tmp_path / "dev"
    ctx = SimpleNamespace(config=SimpleNamespace(
        builder_scratch_root=str(scratch), builder_graduate_root=str(grad),
        builder_allowed_roots=[]), cwd=tmp_path)
    return ctx, p, s


def test_respond_list_empty_then_populated(env):
    ctx, p, _ = env
    assert "don't have any builder projects" in vui.respond(ctx, "list")
    p.new_sandbox_project("snake", ctx.config.builder_scratch_root, description="a snake game")
    out = vui.respond(ctx, "list")
    assert "snake" in out and "a snake game" in out and "current" in out


def test_respond_new_and_switch(env):
    ctx, p, _ = env
    out = vui.respond(ctx, "new", "snake")
    assert "snake" in out and p.active()["name"] == "snake"
    vui.respond(ctx, "new", "tetris")
    assert p.active()["name"] == "tetris"
    out = vui.respond(ctx, "switch", "snake")
    assert "snake" in out and p.active()["name"] == "snake"
    # unknown name lists what exists
    out = vui.respond(ctx, "switch", "nope")
    assert "don't have a builder project called" in out and "snake" in out


def test_respond_new_without_sandbox(env):
    ctx, _, _ = env
    ctx.config.builder_scratch_root = ""
    assert "No builder sandbox" in vui.respond(ctx, "new", "x")


def test_respond_graduate_suggest_then_needs_root(env):
    ctx, p, _ = env
    p.new_sandbox_project("snake", ctx.config.builder_scratch_root)
    out = vui.respond(ctx, "graduate", "")   # suggest
    assert "graduate it as" in out
    ctx.config.builder_graduate_root = ""
    assert "No graduation folder" in vui.respond(ctx, "graduate", "")


def test_respond_help_and_working(env):
    ctx, p, _ = env
    assert "start a builder task" in vui.respond(ctx, "help")
    assert "no current builder project" in vui.respond(ctx, "working").lower()
    p.new_sandbox_project("snake", ctx.config.builder_scratch_root)
    assert "snake" in vui.respond(ctx, "working")


def test_respond_cancel_none_and_status_empty(env):
    ctx, _, _ = env
    assert "Nothing's building" in vui.respond(ctx, "cancel")
    assert "no builds yet" in vui.respond(ctx, "status")
