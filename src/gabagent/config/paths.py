from pathlib import Path
import os


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    p = base / "gabagent"
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    p = base / "gabagent"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sessions_dir(cwd: Path | None = None) -> Path:
    if cwd is None:
        cwd = Path.cwd()
    escaped = str(cwd).replace("/", "-").lstrip("-")
    p = config_dir() / "projects" / escaped / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def memory_file(cwd: Path | None = None) -> Path:
    if cwd is None:
        cwd = Path.cwd()
    escaped = str(cwd).replace("/", "-").lstrip("-")
    d = config_dir() / "projects" / escaped
    d.mkdir(parents=True, exist_ok=True)
    return d / "memory.md"


def persona_dir() -> Path:
    """Global (cwd-independent) home for the self-learning persona layer: seed.md, INDEX.md,
    journal.md. Deliberately ignores cwd — there is ONE Aria across every project/session."""
    p = data_dir() / "persona"
    p.mkdir(parents=True, exist_ok=True)
    return p


def history_file() -> Path:
    return config_dir() / "history"


def rate_limit_file() -> Path:
    return config_dir() / "rate_limit.json"


def web_cache_dir() -> Path:
    p = config_dir() / "web_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return config_dir() / "settings.json"
