"""P1 (read path) for TMI: the TmiManager recall facade + the gated wiring into _voice_system.
With tmi.enabled=False the recall path must stay byte-identical to the legacy persona/memory briefs."""
import types

import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.tmi.manager import TmiManager


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    # Persona + TMI room stores live under data_dir(); project memory under config_dir().
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _ctx(tmp_path, *, enabled: bool, room_id=None):
    from gabagent.agent.context import AgentContext
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": enabled})
    ctx = AgentContext(
        config=cfg,
        client=types.SimpleNamespace(),
        rate_limiter=types.SimpleNamespace(),
        session=types.SimpleNamespace(),
        session_id="s1",
        cwd=tmp_path,
    )
    ctx.room_id = room_id
    ctx.command_catalog = None  # so _capability_brief is empty and doesn't clutter the prompt
    return ctx


# --- TmiManager tiers -----------------------------------------------------

def test_tier0_brief_delegates_to_persona(isolated):
    from gabagent.config.paths import persona_dir
    (persona_dir() / "INDEX.md").write_text("- likes terse replies\n", encoding="utf-8")
    brief = TmiManager(_ctx(isolated, enabled=True)).tier0_brief()
    assert "likes terse replies" in brief  # the shared identity = persona INDEX


def test_tier1_reads_room_store_keyed_by_room_id(isolated):
    from gabagent.config.paths import room_dir
    (room_dir("living_room") / "memory.md").write_text("the couch faces the window", encoding="utf-8")
    ctx = _ctx(isolated, enabled=True, room_id="living_room")
    assert "couch faces the window" in TmiManager(ctx).tier1_brief()


def test_tier1_reads_through_to_legacy_cwd_memory_when_room_empty(isolated):
    from gabagent.session.memory import MemoryManager
    ctx = _ctx(isolated, enabled=True, room_id="kitchen")  # no room store written
    MemoryManager(ctx.cwd).append("the project uses pytest")
    assert "uses pytest" in TmiManager(ctx).tier1_brief()  # read-through surfaced it


def test_tier1_prefers_room_store_over_legacy(isolated):
    from gabagent.config.paths import room_dir
    from gabagent.session.memory import MemoryManager
    ctx = _ctx(isolated, enabled=True, room_id="den")
    MemoryManager(ctx.cwd).append("LEGACY note")
    (room_dir("den") / "memory.md").write_text("ROOM note", encoding="utf-8")
    out = TmiManager(ctx).tier1_brief()
    assert "ROOM note" in out and "LEGACY" not in out


def test_tier1_caps_length(isolated):
    from gabagent.config.paths import room_dir
    (room_dir("big") / "memory.md").write_text("x" * 5000, encoding="utf-8")
    out = TmiManager(_ctx(isolated, enabled=True, room_id="big")).tier1_brief(limit=100)
    assert len(out) <= 101 and out.startswith("…")


# --- _voice_system gating: OFF = legacy wording, ON = tiered wording ------

def test_voice_system_off_uses_legacy_memory_brief(isolated):
    from gabagent.voice.turn import _voice_system
    from gabagent.session.memory import MemoryManager
    ctx = _ctx(isolated, enabled=False, room_id="living_room")
    MemoryManager(ctx.cwd).append("legacy recall content")
    s = _voice_system(ctx)
    assert "What you remember about this project" in s  # legacy wording
    assert "What you remember in this room" not in s


def test_voice_system_on_uses_tiered_room_brief(isolated):
    from gabagent.voice.turn import _voice_system
    from gabagent.config.paths import room_dir
    ctx = _ctx(isolated, enabled=True, room_id="living_room")
    (room_dir("living_room") / "memory.md").write_text("room-scoped recall", encoding="utf-8")
    s = _voice_system(ctx)
    assert "What you remember in this room" in s
    assert "room-scoped recall" in s
