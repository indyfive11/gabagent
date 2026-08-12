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


def tmi_dir() -> Path:
    """Root of the Tiered-Memory-Indexing store (cwd-independent, like persona). Tier 0 (shared across
    every Aria process) lives in tmi/tier0/; per-room Tier 1 in tmi/rooms/<room_id>/."""
    p = data_dir() / "tmi"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tier0_dir() -> Path:
    """Tier-0 store: the consolidated identity/facts SHARED across all Aria processes (every room).
    cwd- AND room-independent — the cross-room 'one Aria'."""
    p = tmi_dir() / "tier0"
    p.mkdir(parents=True, exist_ok=True)
    return p


def room_dir(room_id: str | None = None) -> Path:
    """Tier-1 (per-room) store. `room_id` is the durable per-room key; when unset (single-room install
    / TUI) it falls back to a stable 'default' bucket so behavior is unchanged. The key is sanitized to
    one safe path segment since it can come from config or an /attach payload."""
    key = (room_id or "default").strip() or "default"
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in key)
    p = tmi_dir() / "rooms" / safe
    p.mkdir(parents=True, exist_ok=True)
    return p


def stratum_dir() -> Path:
    """Root of the Stratum store (data_dir, machine-wide). Created lazily by the first save so a
    disabled install touches nothing."""
    return data_dir() / "stratum"


def observed_habits_file() -> Path:
    """Stratum Tier-0.5 SHARED observed-habits store. NOT created here — the store writes it lazily
    on first accretion, keeping `stratum.enabled=False` free of any new files."""
    return stratum_dir() / "observed_habits.md"


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
