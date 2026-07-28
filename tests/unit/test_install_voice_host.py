"""Layer-C voice-host role (the §10d advertise write) — the graft: refuse-on-loopback + co-write voice_host.

The invariant under test is that `voice_advertise:true` can NEVER be persisted alongside a loopback bind
(which would advertise nothing and make the satellite-side remedy text lie), and that the installer's notion
of "loopback" is literally the runtime advertiser's `_is_loopback` — a single source of truth.
"""
from __future__ import annotations

import pytest

from gabagent.config import loader
from gabagent.config.models import GabAgentConfig
from gabagent.install import voice_host, workstation
from gabagent.install.voice_host import LoopbackRefused, RefuseAdvertise, enable_voice_advertise
from gabagent.voice import advertiser


def test_loopback_predicate_is_the_runtime_source_of_truth():
    # The graft's whole point: installer and runtime can't drift on what "loopback" means.
    assert voice_host._is_loopback is advertiser._is_loopback


def test_lan_host_writes_advertise_and_host():
    cfg = GabAgentConfig()  # voice_host defaults to 127.0.0.1, voice_advertise False
    result = enable_voice_advertise(cfg, host="192.0.2.10")
    assert cfg.voice_advertise is True
    assert cfg.voice_host == "192.0.2.10"
    assert result.changed is True
    assert result.host == "192.0.2.10"


def test_room_id_written_when_given():
    cfg = GabAgentConfig()
    result = enable_voice_advertise(cfg, host="192.0.2.10", room_id="roomA")
    assert cfg.voice_room_id == "roomA"
    assert result.room_id == "roomA"


def test_default_config_no_host_refuses_and_writes_nothing():
    cfg = GabAgentConfig()  # loopback default, no --host supplied
    with pytest.raises(LoopbackRefused):
        enable_voice_advertise(cfg)
    # NOTHING mutated on refusal — the silent no-op can never be persisted.
    assert cfg.voice_advertise is False
    assert cfg.voice_host == "127.0.0.1"


@pytest.mark.parametrize("bad", ["127.0.0.1", "::1", "localhost", "", "  "])
def test_explicit_loopback_host_refuses(bad):
    cfg = GabAgentConfig()
    with pytest.raises(LoopbackRefused):
        enable_voice_advertise(cfg, host=bad)
    assert cfg.voice_advertise is False


def test_existing_nonloopback_bind_wins_over_diverging_host():
    # Fork (a): an explicit non-loopback voice_host is the operator's deliberate bind — a diverging --host
    # must NOT silently relocate it (voice_host is the server's listen address, not just the advertised one).
    cfg = GabAgentConfig()
    cfg.voice_host = "192.0.2.99"                       # operator bound a specific NIC
    result = enable_voice_advertise(cfg, host="192.0.2.50")   # detection picked the default-route NIC
    assert cfg.voice_host == "192.0.2.99"              # kept — NOT clobbered
    assert cfg.voice_advertise is True                 # advertising still enabled on the kept bind
    assert result.host == "192.0.2.99"
    assert any("divergence" in n for n in result.notes)   # but the operator is warned


def test_existing_nonloopback_bind_matching_host_no_divergence():
    cfg = GabAgentConfig()
    cfg.voice_host = "192.0.2.50"
    result = enable_voice_advertise(cfg, host="192.0.2.50")
    assert not any("divergence" in n for n in result.notes)
    assert cfg.voice_host == "192.0.2.50"


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_wildcard_host_refused(wildcard):
    cfg = GabAgentConfig()
    with pytest.raises(RefuseAdvertise):
        enable_voice_advertise(cfg, host=wildcard)
    assert cfg.voice_advertise is False                # nothing written


def test_idempotent_second_call_no_change():
    cfg = GabAgentConfig()
    enable_voice_advertise(cfg, host="192.0.2.10")
    second = enable_voice_advertise(cfg, host="192.0.2.10")
    assert second.changed is False  # already enabled → no churn


def test_zeroconf_absent_surfaces_no_mdns_note(monkeypatch):
    monkeypatch.setattr(voice_host.importlib.util, "find_spec", lambda name: None)
    cfg = GabAgentConfig()
    result = enable_voice_advertise(cfg, host="192.0.2.10")
    assert result.zeroconf_present is False
    assert any("no-mdns" in n for n in result.notes)
    # ...but the write still happens — the note is non-fatal.
    assert cfg.voice_advertise is True


# --- CLI seam (the cross-process entry Layer B invokes) -------------------------------------------------

def test_cli_enable_voice_host_writes_and_saves(tmp_path, monkeypatch):
    sf = tmp_path / "cfg" / "settings.json"
    monkeypatch.setattr(loader, "settings_file", lambda: sf)
    rc = workstation.main(["--enable-voice-host", "--host", "192.0.2.10", "--room-id", "roomB"])
    assert rc == 0
    written = loader.load_config()
    assert written.voice_advertise is True
    assert written.voice_host == "192.0.2.10"
    assert written.voice_room_id == "roomB"


def test_cli_enable_voice_host_loopback_refuses_nonzero(tmp_path, monkeypatch):
    sf = tmp_path / "cfg" / "settings.json"
    monkeypatch.setattr(loader, "settings_file", lambda: sf)
    rc = workstation.main(["--enable-voice-host", "--host", "127.0.0.1"])
    assert rc == 2  # non-zero so the Layer-B subprocess sees the refusal
    assert not sf.exists()  # nothing written


def test_cli_enable_voice_host_missing_host_refuses(tmp_path, monkeypatch):
    sf = tmp_path / "cfg" / "settings.json"
    monkeypatch.setattr(loader, "settings_file", lambda: sf)
    rc = workstation.main(["--enable-voice-host"])  # falls back to loopback config default → refuse
    assert rc == 2
    assert not sf.exists()
