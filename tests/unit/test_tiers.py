from pathlib import Path
import pytest
from gabagent.permissions.tiers import tier_of
from gabagent.config.models import GabAgentConfig


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _cfg():
    return GabAgentConfig(api_key="test")


def test_reads_are_tier1(home):
    cfg = _cfg()
    for t in ("read_file", "glob", "grep", "git_status", "git_diff", "web_search", "read_claude_memory"):
        assert tier_of(t, {"path": "x"}, home / "proj", cfg) == 1


def test_write_into_scratch_is_tier1(home):
    cfg = _cfg()
    dest = home / "voice-scratch" / "note.txt"
    assert tier_of("write_file", {"path": str(dest), "content": "hi"}, home / "proj", cfg) == 1


def test_write_into_documents_is_tier1(home):
    cfg = _cfg()
    dest = home / "Documents" / "memo.md"
    assert tier_of("write_file", {"path": str(dest), "content": "hi"}, home / "proj", cfg) == 1


def test_project_edit_is_tier2(home):
    cfg = _cfg()
    proj = home / "proj"
    assert tier_of("edit", {"path": "src/app.py"}, proj, cfg) == 2
    assert tier_of("write_file", {"path": str(proj / "README.md"), "content": "x"}, proj, cfg) == 2


def test_write_outside_zones_is_tier3(home):
    cfg = _cfg()
    dest = home / "other" / "x.txt"
    assert tier_of("write_file", {"path": str(dest), "content": "x"}, home / "proj", cfg) == 3


def test_sensitive_paths_are_tier3(home):
    cfg = _cfg()
    proj = home / "proj"
    assert tier_of("write_file", {"path": "/etc/hosts", "content": "x"}, proj, cfg) == 3
    assert tier_of("write_file", {"path": str(home / ".bashrc"), "content": "x"}, proj, cfg) == 3


def test_bash_is_tier3(home):
    cfg = _cfg()
    assert tier_of("bash", {"command": "ls"}, home / "proj", cfg) == 3
    assert tier_of("bash", {"command": "rm -rf /"}, home / "proj", cfg) == 3


def test_git_commit_is_tier2(home):
    assert tier_of("git_commit", {"message": "wip"}, home / "proj", _cfg()) == 2


def test_git_branch_list_vs_switch(home):
    cfg = _cfg()
    assert tier_of("git_branch", {"action": "list"}, home / "proj", cfg) == 1
    assert tier_of("git_branch", {"action": "switch", "name": "x"}, home / "proj", cfg) == 2


def test_reconfigure_safe_zone_is_tier3(home):
    assert tier_of("reconfigure_safe_zone", {}, home / "proj", _cfg()) == 3


def test_unknown_tool_defaults_tier3(home):
    assert tier_of("frobnicate", {}, home / "proj", _cfg()) == 3


def test_extra_safe_zone_from_config(home):
    cfg = _cfg()
    cfg.voice_safe_zones = [str(home / "extra")]
    dest = home / "extra" / "f.txt"
    assert tier_of("write_file", {"path": str(dest), "content": "x"}, home / "proj", cfg) == 1


def test_unknown_run_command_is_tier1_not_keyboard(home):
    # A hallucinated/unknown command id must not force a keyboard confirm — it can't execute;
    # the run_command tool rejects it cleanly and the model self-corrects.
    from gabagent.commands.catalog import CommandCatalog
    cfg = _cfg()
    cat = CommandCatalog()
    assert tier_of("run_command", {"command_id": "media.play_movie"}, home, cfg, cat) == 1
    assert tier_of("run_command", {"command_id": "nope"}, home, cfg, None) == 1


def test_postmortem_log_is_tier1_internal(home):
    # Internal bookkeeping/telemetry must auto-run, never prompt the user.
    assert tier_of("postmortem_log", {"title": "x", "description": "y"}, home, _cfg()) == 1


def test_memory_is_tier1_low_friction(home):
    # The agent's own project memory: reading is a read, writing is reversible ("forget that").
    cfg = _cfg()
    assert tier_of("memory_read", {}, home, cfg) == 1
    assert tier_of("memory_write", {"content": "the user likes jazz"}, home, cfg) == 1
