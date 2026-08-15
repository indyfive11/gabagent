import types
import pytest
from gabagent.voice.commands import (
    detect_meta_command, undo_last, answer_query, current_brain,
)
from gabagent.voice.session import VoiceSession
from gabagent.config.models import GabAgentConfig


@pytest.mark.parametrize("text", [
    "make local the floor",
    "use local as the fallback",
    "set the default to local",
    "local as the bottom rung",
    "use local as the baseline",
])
def test_floor_on_phrases_detect_floor_local(text):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "floor" and mc.value == "local"


@pytest.mark.parametrize("text", [
    "drop local",
    "use Aria as the floor",
    "make the default Aria",
    "turn off the local floor",
    "unload local",
])
def test_floor_off_phrases_detect_floor_aria(text):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "floor" and mc.value == "aria"


@pytest.mark.parametrize("text", ["switch to local", "use local", "load devstral"])
def test_bare_local_switch_is_still_exclusive_brain(text):
    # No floor/fallback qualifier → the existing EXCLUSIVE local_mode switch, not the floor toggle.
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "brain" and mc.value == "local"


@pytest.mark.parametrize("text", ["turn on local", "turn local on", "enable local", "activate local"])
def test_turn_on_local_is_floor(text):
    # Intuitive "turn on local" → the FLOOR (escalates), the cross-backend default.
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "floor" and mc.value == "local"


@pytest.mark.parametrize("text", ["use only local", "only use local", "local only", "exclusively local"])
def test_only_local_is_exclusive(text):
    # "use only local" → EXCLUSIVE local (no escalation).
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "brain" and mc.value == "local"


@pytest.mark.parametrize("text", ["turn off local", "turn local off", "disable local", "stop local"])
def test_turn_off_local_drops_to_aria(text):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "floor" and mc.value == "aria"


@pytest.mark.parametrize("text", ["go back to the cloud", "use claude", "switch to arya model", "use the aria brain"])
def test_explicit_cloud_switch(text):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "brain" and mc.value == "cloud"


@pytest.mark.parametrize("text", ["switch to Aria", "use arya", "use aria", "switch to arya"])
def test_bare_name_is_not_a_cloud_switch(text):
    # Require-noun tightening: a BARE assistant/model/project name is not a brain switch (it collides
    # with the assistant's name and the "work on <project>" command) → falls through to the model.
    assert detect_meta_command(text) is None


def _ctx(tmp_path, **kw):
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(
        config=cfg, cwd=tmp_path, local_mode=False, force_model=False,
        active_model=None, voice_audit_path=None, voice_session=None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def test_detect_brain_switch():
    assert detect_meta_command("switch to local").value == "local"
    assert detect_meta_command("go local now").value == "local"
    assert detect_meta_command("launch the local model").value == "local"
    assert detect_meta_command("back to cloud").value == "cloud"
    assert detect_meta_command("use claude please").value == "cloud"
    assert detect_meta_command("go back online").value == "cloud"


def test_detect_quiet_shutup_is_terse_meta():
    # "Shut up / be quiet" → a deterministic quiet meta (no LLM, terse pointer), incl. "shut up voice mode".
    for phrase in (
        "shut up", "shut up voice mode", "be quiet", "quiet down", "stop talking",
        "stop yapping", "hush", "pipe down", "zip it", "that's enough", "enough talking",
        "enough already",
    ):
        mc = detect_meta_command(phrase)
        assert mc is not None and mc.kind == "quiet", phrase


def test_detect_quiet_does_not_eat_media_stop():
    # Strict: a media stop/pause with an object must NOT be swallowed by the quiet meta (bare "stop"
    # and "stop the music" carry no quiet handler → fall through to normal routing for the real stop).
    for phrase in ("stop the music", "pause the movie", "stop"):
        mc = detect_meta_command(phrase)
        assert mc is None or mc.kind != "quiet", phrase


def test_cloud_switch_by_name_requires_a_noun():
    # Names (aria/arya/gab) require an explicit brain/model noun, so they don't collide with the
    # assistant's own name or the "work on <project>" command.
    assert detect_meta_command("switch to ARIA model").value == "cloud"
    assert detect_meta_command("use the aria brain").value == "cloud"
    assert detect_meta_command("go to gab brain").value == "cloud"
    # Bare names are NOT a switch (fall through to the model / project path).
    assert detect_meta_command("let's stop this and go back to Aria") is None
    assert detect_meta_command("use aria") is None
    assert detect_meta_command("switch to arya") is None
    # Not a switch — just mentioning the name.
    assert detect_meta_command("tell me about aria") is None


def test_detect_status_query_natural_variants():
    for phrase in (
        "are you running local",
        "are you on a local model",
        "verify your model",
        "confirm which model you're on",
        "where are you running",
    ):
        m = detect_meta_command(phrase)
        assert m is not None and m.kind == "query" and m.value == "model", phrase


def test_detect_no_false_positive():
    assert detect_meta_command("open the local file") is None
    assert detect_meta_command("read the local config and summarize") is None
    assert detect_meta_command("what does this function do") is None


def test_detect_undo():
    assert detect_meta_command("undo that").kind == "undo"
    assert detect_meta_command("revert that change").kind == "undo"
    assert detect_meta_command("roll back the last edit").kind == "undo"


def test_detect_queries():
    assert detect_meta_command("what model are you on").value == "model"
    assert detect_meta_command("are you local or cloud").value == "model"
    assert detect_meta_command("what folder are you in").value == "where"
    assert detect_meta_command("what can you touch").value == "where"
    assert detect_meta_command("recap what you've done").value == "recap"


def test_undo_restores_prior_content(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("new content")
    vs = VoiceSession("s", None)
    vs.push_undo(str(f), b"old content")
    ctx = _ctx(tmp_path, voice_session=vs)
    msg = undo_last(ctx)
    assert f.read_text() == "old content"
    assert "Reverted" in msg


def test_undo_removes_new_file(tmp_path):
    f = tmp_path / "created.txt"
    f.write_text("created by voice")
    vs = VoiceSession("s", None)
    vs.push_undo(str(f), None)
    ctx = _ctx(tmp_path, voice_session=vs)
    undo_last(ctx)
    assert not f.exists()


def test_undo_nothing(tmp_path):
    ctx = _ctx(tmp_path, voice_session=VoiceSession("s", None))
    assert "nothing" in undo_last(ctx).lower()


def test_query_model_reports_brain(tmp_path):
    ctx = _ctx(tmp_path)
    ans = answer_query(ctx, "model")
    assert "arya" in ans and "cloud" in ans


def test_query_model_local(tmp_path):
    ctx = _ctx(tmp_path, local_mode=True)
    ctx.config.local_model = "devstral:24b"
    ans = answer_query(ctx, "model")
    assert "local" in ans and "devstral:24b" in ans


def test_current_brain_local(tmp_path):
    ctx = _ctx(tmp_path, local_mode=True)
    ctx.config.local_model = "devstral:24b"
    assert current_brain(ctx) == "devstral:24b"
