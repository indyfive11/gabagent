from __future__ import annotations
import json
from pathlib import Path
from .models import GabAgentConfig
from .paths import settings_file


_config: GabAgentConfig | None = None


def load_config(overrides: dict | None = None) -> GabAgentConfig:
    global _config
    sf = settings_file()
    file_data: dict = {}
    if sf.exists():
        try:
            file_data = json.loads(sf.read_text())
        except Exception:
            pass
    if overrides:
        file_data.update(overrides)
    _config = GabAgentConfig(**file_data)
    return _config


def get_config() -> GabAgentConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def save_config(cfg: GabAgentConfig) -> None:
    global _config
    sf = settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(exclude_unset=False)
    sf.write_text(json.dumps(data, indent=2))
    # settings.json holds plaintext secrets (api keys, jellyfin password, voice auth token) — keep it
    # owner-only so it isn't world-readable on a multi-user box. Best-effort: a filesystem that can't chmod
    # (e.g. some network mounts) must not break saving. Mirrors installkit's Phase-3 0600 env-file posture.
    try:
        sf.chmod(0o600)
        sf.parent.chmod(0o700)
    except OSError:
        pass
    _config = cfg  # keep the in-process cache coherent with what was just written


def bootstrap_config() -> GabAgentConfig:
    sf = settings_file()
    if not sf.exists():
        cfg = GabAgentConfig()
        save_config(cfg)
        return cfg
    return load_config()
