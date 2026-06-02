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


class TidalConfig(BaseModel):
    """TIDAL via a local Mopidy + mopidy-tidal server (first-party provider).

    Mopidy exposes an HTTP JSON-RPC API; this skill drives search → queue → play and
    transport over it. See the setup note for installing/authorizing mopidy-tidal.
    """
    enabled: bool = True
    rpc_url: str = "http://localhost:6680/mopidy/rpc"


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
    attestation: AttestationConfig = Field(default_factory=AttestationConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    tidal: TidalConfig = Field(default_factory=TidalConfig)
