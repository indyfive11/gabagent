import types
import pytest
from gabagent.voice.commands import (
    detect_meta_command, undo_last, answer_query, current_brain,
)
from gabagent.voice.session import VoiceSession
from gabagent.config.models import GabAgentConfig


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
    assert "arya" in answer_query(ctx, "model")


def test_current_brain_local(tmp_path):
    ctx = _ctx(tmp_path, local_mode=True)
    ctx.config.local_model = "devstral:24b"
    assert current_brain(ctx) == "devstral:24b"
