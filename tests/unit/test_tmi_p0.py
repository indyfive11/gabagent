"""P0 (foundation) for the Tiered-Memory-Indexing layer: config surface, path helpers, and the
durable room_id on AgentContext. All behavior-neutral — TMI is OFF by default, so these only assert
the scaffolding exists with safe defaults."""
import types

from gabagent.config.models import GabAgentConfig, TmiConfig
from gabagent.config import paths


# --- config surface: default-off, safe caps -------------------------------

def test_tmi_defaults_are_off_and_safe():
    t = TmiConfig()
    assert t.enabled is False  # master switch off => byte-identical to today
    assert (t.tier0_soft_cap, t.tier0_hard_cap) == (20, 50)
    assert (t.habit_count, t.habit_window_days) == (3, 14)  # 3-across-14-days, not 3-in-a-week
    assert t.decay_halflife_days == 30
    assert t.tier0_exclude_rooms == []  # allow-all default (privacy switch built, off)
    assert t.reconcile_interval_secs == 0  # shutdown-trigger only in v1


def test_tmi_attached_to_main_config_with_defaults():
    cfg = GabAgentConfig(api_key="t")
    assert isinstance(cfg.tmi, TmiConfig)
    assert cfg.tmi.enabled is False


def test_tmi_config_is_user_overridable():
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": True, "tier0_hard_cap": 40,
                                          "tier0_exclude_rooms": ["bedroom"]})
    assert cfg.tmi.enabled is True
    assert cfg.tmi.tier0_hard_cap == 40
    assert cfg.tmi.tier0_exclude_rooms == ["bedroom"]


# --- path helpers ---------------------------------------------------------

def test_tier0_and_room_dirs_resolve_under_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.tmi_dir() == tmp_path / "gabagent" / "tmi"
    assert paths.tier0_dir() == tmp_path / "gabagent" / "tmi" / "tier0"
    assert paths.tier0_dir().is_dir()  # created


def test_room_dir_defaults_to_shared_bucket_when_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.room_dir(None).name == "default"
    assert paths.room_dir("").name == "default"
    assert paths.room_dir("   ").name == "default"


def test_room_dir_keys_distinct_rooms_separately(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    a, b = paths.room_dir("living_room"), paths.room_dir("kitchen")
    assert a != b
    assert a.name == "living_room" and a.is_dir()


def test_room_dir_sanitizes_unsafe_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # a room_id can arrive from /attach; it must stay a single safe path segment
    p = paths.room_dir("../../etc/evil room")
    assert "/" not in p.name and ".." != p.name
    assert p.parent == tmp_path / "gabagent" / "tmi" / "rooms"


# --- AgentContext.room_id -------------------------------------------------

def test_agent_context_room_id_defaults_none():
    from gabagent.agent.context import AgentContext
    ctx = AgentContext(
        config=GabAgentConfig(api_key="t"),
        client=types.SimpleNamespace(),
        rate_limiter=types.SimpleNamespace(),
        session=types.SimpleNamespace(),
        session_id="s1",
    )
    assert ctx.room_id is None
