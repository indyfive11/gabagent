from __future__ import annotations
import json
import os
from pydantic_settings import JsonConfigSettingsSource
from .models import GabAgentConfig
from .paths import settings_file


_config: GabAgentConfig | None = None


class _FileBackedConfig(GabAgentConfig):
    """A GabAgentConfig that ALSO reads settings.json — as a settings SOURCE ranked below env.

    Only `load_config` constructs this. A bare `GabAgentConfig()` stays file-free (env + defaults),
    preserving the historical boundary that *only* load_config reads settings.json — so the ~dozens of
    bare constructions across the code/tests keep yielding defaults regardless of a real on-disk file.

    Precedence (highest→lowest): init/CLI-overrides > env > dotenv > settings.json > file-secrets > defaults.
    This is the fix for the env-shadow bug: previously the whole settings.json was passed as INIT kwargs
    (the highest source), so a saved default like voice_auth_token="" shadowed GABAI_VOICE_AUTH_TOKEN from
    the systemd EnvironmentFile and the LAN brain ran with no effective bearer auth.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # settings_file() is resolved FRESH on every construction (this classmethod runs per __init__),
        # so the XDG-relative path stays dynamic and tests that monkeypatch `loader.settings_file` are
        # honored. The JSON source MUST be constructed here (it reads the file in __init__) — never at
        # module/class scope, or the path/data would freeze at import. Keyword param names are copied
        # verbatim: pydantic-settings calls this by keyword and a rename TypeErrors on first load.
        json_src = JsonConfigSettingsSource(settings_cls, json_file=settings_file())
        return (init_settings, env_settings, dotenv_settings, json_src, file_secret_settings)


def load_config(overrides: dict | None = None) -> GabAgentConfig:
    global _config
    # `overrides` become true INIT kwargs — the highest-priority source — so CLI flags (--api-key,
    # --model, --set-claude-key) correctly beat both env and the file. The file is read by the source
    # wired above, NOT passed as kwargs (that was the shadow bug).
    _config = _FileBackedConfig(**(overrides or {}))
    return _config


def get_config() -> GabAgentConfig:
    global _config
    if _config is None:
        _config = load_config()
    return _config


def _env_present_fields() -> set[str]:
    """Top-level field env-var names (uppercased, GABAI_-prefixed) that are SET AND NON-EMPTY.

    Case-insensitive (pydantic env matching is), and empty values are excluded so it composes with
    env_ignore_empty: an empty GABAI_X= is treated as unset, so its file value must persist, not be
    skipped. Nested-model fields (jellyfin, claude, …) have no working env var — no env_nested_delimiter
    is set — so they never appear here and always persist to the file.
    """
    return {
        k.upper()
        for k, v in os.environ.items()
        if k.upper().startswith("GABAI_") and v != ""
    }


def save_config(cfg: GabAgentConfig) -> None:
    global _config
    sf = settings_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    # Preserve-merge (not full-replace): start from what's already on disk, then overwrite each top-level
    # field with the config's value — EXCEPT fields whose GABAI_<FIELD> env var is set+non-empty. Under the
    # env-over-file precedence, load_config() returns env-sourced values; dumping them wholesale would bake
    # the EnvironmentFile's secrets (GABAI_API_KEY, GABAI_VOICE_AUTH_TOKEN, …) into this plaintext file. So
    # we skip env-authoritative fields — the env stays the single source for them — while leaving any value
    # already in the file untouched (a save triggered by an unrelated field, e.g. "/local floor", no longer
    # silently strips voice_host/voice_advertise). A secret the user genuinely wrote into the file stays
    # (that is the existing 0600 posture, not a new exposure).
    existing: dict = {}
    if sf.exists():
        try:
            existing = json.loads(sf.read_text())
        except Exception:
            existing = {}
    env_present = _env_present_fields()
    for field, val in cfg.model_dump(exclude_unset=False).items():
        if ("GABAI_" + field.upper()) in env_present:
            continue  # env is authoritative for this field — don't copy it into the file
        existing[field] = val
    sf.write_text(json.dumps(existing, indent=2))
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
