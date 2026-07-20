"""save_config writes settings.json owner-only (0600) — it holds plaintext secrets."""
from __future__ import annotations

import stat

from gabagent.config import loader
from gabagent.config.models import GabAgentConfig


def test_save_config_is_owner_only(tmp_path, monkeypatch):
    sf = tmp_path / "cfg" / "settings.json"
    monkeypatch.setattr(loader, "settings_file", lambda: sf)
    loader.save_config(GabAgentConfig())
    mode = stat.S_IMODE(sf.stat().st_mode)
    assert mode == 0o600, f"settings.json should be 0600, got {oct(mode)}"
    dir_mode = stat.S_IMODE(sf.parent.stat().st_mode)
    assert dir_mode == 0o700, f"config dir should be 0700, got {oct(dir_mode)}"
