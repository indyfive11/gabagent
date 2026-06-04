from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HookEntry(BaseModel):
    matcher: str = "*"
    command: str


class HooksConfig(BaseModel):
    PreToolUse: list[HookEntry] = Field(default_factory=list)
    PostToolUse: list[HookEntry] = Field(default_factory=list)
    UserPromptSubmit: list[HookEntry] = Field(default_factory=list)
    Stop: list[HookEntry] = Field(default_factory=list)
    SessionStart: list[HookEntry] = Field(default_factory=list)


class PermissionsConfig(BaseModel):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    mode: str = "default"


class MCPServer(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    transport: str = "stdio"
    url: str | None = None


class LSPConfig(BaseModel):
    servers: dict[str, str] = Field(default_factory=dict)


class RouterConfig(BaseModel):
    enabled: bool = True
    classifier_enabled: bool = True
    simple_model: str = "arya"
    complex_model: str = "claude-sonnet-4-5"


class AttestationConfig(BaseModel):
    """How skill plugins are vetted before they may run."""
    reviewer: str = "claude_api"          # claude_api | claude_code_bridge | off
    model: str = ""                       # default: router.complex_model
    require_keyboard_for_tier3: bool = True
    auto_reject_obfuscation: bool = False  # True => bash -c / eval / inline-code rejected outright


class JellyfinConfig(BaseModel):
    """Jellyfin media-server integration (first-party provider)."""
    enabled: bool = True
    base_url: str = "http://localhost:8096"
    api_key: str = ""                     # Dashboard → API Keys
    user_id: str = ""                     # optional: enables played/unwatched filtering
    rating_threshold: float = 7.0         # default minimum CommunityRating (IMDb 0–10)
    username: str = ""                    # optional: hands-free web-player auto-auth (plaintext — opt-in)
    password: str = ""


class TidalConfig(BaseModel):
    """TIDAL via a local Mopidy + mopidy-tidal server (first-party provider).

    Mopidy exposes an HTTP JSON-RPC API; this skill drives search → queue → play and
    transport over it. See the setup note for installing/authorizing mopidy-tidal.
    """
    enabled: bool = True
    rpc_url: str = "http://localhost:6680/mopidy/rpc"


class DesktopConfig(BaseModel):
    """KDE/Wayland desktop control (first-party provider)."""
    # Friendly monitor names → KWin connector (e.g. {"hisense": "DP-1"}). Display make/model isn't
    # exposed by kscreen-doctor, so this lets a user say "move it to the Hisense" and have it resolve.
    # Keys are matched case-insensitively. Host-specific → set in your settings.json, not in source.
    screen_aliases: dict[str, str] = Field(default_factory=dict)


class GabAgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GABAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = ""
    base_url: str = "https://gab.ai/v1"
    model: str = "arya"
    max_context_tokens: int = 120000
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    mcp_servers: dict[str, MCPServer] = Field(default_factory=dict)
    lsp: LSPConfig = Field(default_factory=LSPConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    searxng_url: str = ""
    vim_mode: bool = False
    theme: str = "monokai"
    load_global_claude_md: bool = False
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = ""
    # Voice mode (gab --voice-serve). Empty voice_model ⇒ normal router (arya base,
    # escalate to Claude); a non-empty voice_model pins that single model.
    voice_model: str = ""
    voice_port: int = 8765
    voice_safe_zones: list[str] = Field(default_factory=list)
    voice_passphrase: str = ""
    voice_persona: str = ""
    voice_arm_seconds: int = 120
    voice_debug_log: bool = False  # opt-in per-turn brain-side debug log (keyed by session_id)
    commands_enabled: bool = True  # voice command framework (capability discovery + run_command)
    # Hold playing music at this % cap continuously (not just on speech) so VAD can hear the user over
    # it — the speech-duck drops it deeper, then restores to this cap. 100 disables. Slide down if VAD
    # tuning alone can't keep up. Env: GABAI_MEDIA_AMBIENT_CAP.
    media_ambient_cap: int = 90
    # Floor for an absolute media-volume set (tidal.set_volume): clamp the requested level up to at
    # least this % so "turn the music way down" can't ratchet to inaudible — to actually silence music
    # the user pauses/stops it. Env: GABAI_MEDIA_VOLUME_FLOOR.
    media_volume_floor: int = 5
    # This machine's friendly name, used to tag media sources as LOCAL vs on another device/room (e.g.
    # "EndeavorMain"). Empty → defaults to the hostname at use. The brain only AUTO-ducks/controls media it
    # judges local to this device; remote sources are visible (for future explicit control) but never touched
    # automatically. Env: GABAI_LOCAL_DEVICE.
    local_device: str = ""
    attestation: AttestationConfig = Field(default_factory=AttestationConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    tidal: TidalConfig = Field(default_factory=TidalConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
