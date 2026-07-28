"""Config precedence + save-merge (the env-shadow fix).

Precedence: init/CLI-overrides > env (GABAI_*) > settings.json > defaults. Previously the whole
settings.json was passed as init kwargs, shadowing every env var — so a saved voice_auth_token=""
overrode the systemd EnvironmentFile's GABAI_VOICE_AUTH_TOKEN and the LAN brain ran without auth.
"""
from __future__ import annotations
import json

import pytest

from gabagent.config import loader
from gabagent.config.models import GabAgentConfig


@pytest.fixture
def settings_json(tmp_path, monkeypatch):
    """Point loader.settings_file at a temp settings.json (honored by the file source and save_config)."""
    sf = tmp_path / "settings.json"
    monkeypatch.setattr(loader, "settings_file", lambda: sf)
    return sf


def _write(sf, data: dict):
    sf.write_text(json.dumps(data))


# ── precedence ───────────────────────────────────────────────────────────────

def test_env_beats_file_scalar(settings_json, monkeypatch):
    _write(settings_json, {"voice_port": 7000})
    monkeypatch.setenv("GABAI_VOICE_PORT", "9999")
    assert loader.load_config().voice_port == 9999


def test_env_beats_file_secret(settings_json, monkeypatch):
    # The core security case: a saved empty token must NOT shadow the env token.
    _write(settings_json, {"voice_auth_token": ""})
    monkeypatch.setenv("GABAI_VOICE_AUTH_TOKEN", "ENVTOKEN")
    assert loader.load_config().voice_auth_token == "ENVTOKEN"


def test_file_applies_without_env(settings_json):
    _write(settings_json, {"voice_port": 7000, "voice_auth_token": "FILETOK"})
    cfg = loader.load_config()
    assert cfg.voice_port == 7000
    assert cfg.voice_auth_token == "FILETOK"


def test_overrides_beat_env(settings_json, monkeypatch):
    monkeypatch.setenv("GABAI_VOICE_PORT", "9999")
    assert loader.load_config({"voice_port": 1234}).voice_port == 1234


def test_defaults_when_no_file_no_env(settings_json):
    # settings_json fixture points at a not-yet-created file → missing file must not crash.
    assert not settings_json.exists()
    assert loader.load_config().voice_port == 8765


# ── F1: empty env is treated as unset ────────────────────────────────────────

def test_empty_env_ignored_bool(settings_json, monkeypatch):
    # The live regression: an install's env file sets GABAI_VOICE_ADVERTISE= (empty). It must NOT flip the
    # file's True to False.
    _write(settings_json, {"voice_advertise": True})
    monkeypatch.setenv("GABAI_VOICE_ADVERTISE", "")
    assert loader.load_config().voice_advertise is True


def test_empty_env_ignored_secret(settings_json, monkeypatch):
    _write(settings_json, {"voice_auth_token": "FILETOK"})
    monkeypatch.setenv("GABAI_VOICE_AUTH_TOKEN", "")
    assert loader.load_config().voice_auth_token == "FILETOK"


def test_empty_numeric_env_does_not_raise(settings_json, monkeypatch):
    # F3 softening: an empty numeric env degrades to unset→file/default, not a ValidationError.
    _write(settings_json, {"voice_port": 7000})
    monkeypatch.setenv("GABAI_VOICE_PORT", "")
    assert loader.load_config().voice_port == 7000


def test_malformed_env_raises(settings_json, monkeypatch):
    # A non-empty but invalid env value now fails loud (previously silently shadowed by the file).
    _write(settings_json, {"voice_port": 7000})
    monkeypatch.setenv("GABAI_VOICE_PORT", "notanint")
    with pytest.raises(Exception):
        loader.load_config()


# ── nested + boundary ────────────────────────────────────────────────────────

def test_nested_model_loads_from_file(settings_json):
    _write(settings_json, {"jellyfin": {"base_url": "http://jf.example:8096", "rating_threshold": 8.5}})
    cfg = loader.load_config()
    assert cfg.jellyfin.base_url == "http://jf.example:8096"
    assert cfg.jellyfin.rating_threshold == 8.5


def test_bare_construction_ignores_file(settings_json):
    # The scoping guarantee: only load_config reads the file; a bare GabAgentConfig() stays default+env.
    _write(settings_json, {"voice_port": 7000})
    assert GabAgentConfig().voice_port == 8765


# ── F2-e: save-merge doesn't leak env secrets, preserves existing ────────────

def test_save_does_not_persist_env_secret(settings_json, monkeypatch):
    _write(settings_json, {"voice_auth_token": "FILETOK", "voice_port": 7000})
    monkeypatch.setenv("GABAI_VOICE_AUTH_TOKEN", "ENVSECRET")
    cfg = loader.load_config()  # effective token is ENVSECRET (env wins)
    loader.save_config(cfg)
    on_disk = json.loads(settings_json.read_text())
    # The env secret must NOT be baked into the plaintext file; the file's own value is preserved.
    assert on_disk["voice_auth_token"] == "FILETOK"


def test_save_preserves_unrelated_env_field(settings_json, monkeypatch):
    # A save triggered by one field must not strip another env-authoritative field from the file.
    _write(settings_json, {"voice_host": "192.0.2.10", "local_floor": False})
    monkeypatch.setenv("GABAI_VOICE_HOST", "10.0.0.5")
    cfg = loader.load_config()
    cfg.local_floor = True  # simulate a "/local floor" toggle
    loader.save_config(cfg)
    on_disk = json.loads(settings_json.read_text())
    assert on_disk["local_floor"] is True                 # the intended change persisted
    assert on_disk["voice_host"] == "192.0.2.10"          # env-authoritative field NOT overwritten/stripped


def test_save_writes_non_env_field(settings_json):
    _write(settings_json, {})
    cfg = loader.load_config()
    cfg.voice_persona = "brief"
    loader.save_config(cfg)
    assert json.loads(settings_json.read_text())["voice_persona"] == "brief"
