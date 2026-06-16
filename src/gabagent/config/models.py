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
    # When True, the per-session ladder is assembled ACROSS backends (local → Aria → Claude rungs)
    # so a turn can climb from the cheap floor into the real Anthropic ladder regardless of the
    # `provider` setting, as long as an Anthropic key exists. Flip False to restore the old
    # provider-scoped behavior (the Claude rungs are only reachable on provider="claude").
    cross_backend: bool = True


class Rung(BaseModel):
    """One step on the Claude escalation ladder: a (model, effort) pair.

    `effort=""` means plain / no thinking (the bottom-rung haiku default — effort and
    adaptive thinking 400 on Haiku 4.5). Otherwise one of low|medium|high|max, applied
    only to models that support it (opus/sonnet).

    `backend` names which client serves this rung — "claude" (Anthropic), "gab" (Aria on
    gab.ai), or "local" (Ollama). The persisted `claude.ladder` rungs are all "claude"; the
    local/Aria floor rungs are assembled at runtime by the router (see ModelRouter).
    """
    model: str
    effort: str = ""
    backend: str = "claude"


def _default_ladder() -> list[Rung]:
    # Ascending: least-capable/no-thinking → most-capable/high-effort. The bottom rung's
    # model doubles as the per-turn rung classifier model (cheapest call).
    return [
        Rung(model="claude-haiku-4-5", effort=""),        # rung 0 — bottom: no thinking
        Rung(model="claude-sonnet-4-6", effort="low"),
        Rung(model="claude-sonnet-4-6", effort="high"),
        Rung(model="claude-opus-4-8", effort="medium"),
        Rung(model="claude-opus-4-8", effort="high"),
        Rung(model="claude-opus-4-8", effort="xhigh"),    # agentic-coding default
        Rung(model="claude-opus-4-8", effort="max"),      # rung 6 — top: absolute ceiling (Opus-only)
    ]


class ClaudeConfig(BaseModel):
    """Anthropic ("claudette") backend settings, used when provider == "claude"."""
    api_key: str = ""                     # or set ANTHROPIC_API_KEY in the env
    max_tokens: int = 8192
    ladder: list[Rung] = Field(default_factory=_default_ladder)


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
    # Encodes the user's SOP "never mention other Jellyfin sessions/players unless I explicitly ask." When
    # False (default), actions like close just report what they did and DON'T volunteer that a different
    # (unowned/other-device) session is still playing — the on-demand `jellyfin.now_playing` is the explicit
    # "is anything playing elsewhere?" path. Flip True to restore the volunteered "…but another is still
    # playing" notice. Env: GABAI_JELLYFIN__ANNOUNCE_OTHER_SESSIONS.
    announce_other_sessions: bool = False


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
    # When a movie starts in a window we own, put it full screen on this output. Default "largest"
    # picks the highest-resolution display (the usual TV / main viewing screen) with no host-specific
    # config — keying on size, not a connector, so it travels to any machine. Override with a connector
    # name (DP-1), a `screen_aliases` key, an index, or "" to keep it where it opened. Case-insensitive.
    movie_screen: str = "largest"
    # Auto-fullscreen a movie on play. Turn off to leave movies windowed until you ask for full screen.
    auto_fullscreen_movie: bool = True


class GabAgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GABAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Active LLM backend: "gab" (OpenAI-compatible gab.ai) or "claude" (Anthropic). The
    # local Ollama path is reached via local_model regardless of provider. Env: GABAI_PROVIDER.
    provider: str = "gab"
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
    # When True, the local Ollama model is the persisted BOTTOM RUNG of the escalation ladder
    # (the warm floor): trivial turns run on it, harder turns climb to Aria → Claude. When False,
    # Aria is the floor and local is off. Distinct from the ephemeral `local_mode` (exclusive
    # local, router off). Toggled by "/local floor" | "/local aria" and the voice floor commands.
    local_floor: bool = False
    # Voice mode (gab --voice-serve). Empty voice_model ⇒ normal router (arya base,
    # escalate to Claude); a non-empty voice_model pins that single model.
    voice_model: str = ""
    voice_port: int = 8765
    voice_safe_zones: list[str] = Field(default_factory=list)
    voice_passphrase: str = ""
    voice_persona: str = ""
    voice_arm_seconds: int = 120
    # `/voice on` spawns the voice-agent front-end (mic + wake word) so the brain can hear, pointed at
    # the brain we just started. Empty → auto-resolve the `voice-agent` binary on PATH, then
    # ~/dev/voice-agent/run.sh. Override for a non-standard install. The brain port is passed via
    # GAB_PORT so the front-end ATTACHES to our brain instead of spawning its own.
    voice_agent_cmd: list[str] = Field(default_factory=list)
    voice_debug_log: bool = False  # opt-in per-turn brain-side debug log (keyed by session_id)
    # "Addressed-to-me?" filter: while the wake window is open, an undirected utterance (a curse,
    # thinking aloud, commentary about the assistant) gets NO reply/action. Hybrid: obvious
    # commands/questions skip the check; only ambiguous utterances pay a one-shot classify. Bias is
    # answer-when-unsure so it never eats a command. Env: GABAI_VOICE_INTENT_FILTER.
    voice_intent_filter: bool = True
    commands_enabled: bool = True  # voice command framework (capability discovery + run_command)
    # Hold playing music at this % cap continuously (not just on speech) so VAD can hear the user over
    # it — the speech-duck drops it deeper, then restores to this cap. 100 disables. Slide down if VAD
    # tuning alone can't keep up. Env: GABAI_MEDIA_AMBIENT_CAP.
    media_ambient_cap: int = 90
    # Floor for an absolute media-volume set (tidal.set_volume): clamp the requested level up to at
    # least this % so "turn the music way down" can't ratchet to inaudible — to actually silence music
    # the user pauses/stops it. Env: GABAI_MEDIA_VOLUME_FLOOR.
    media_volume_floor: int = 5
    # Media-control keepalive: after any media command (play/pause/seek/volume/…), ask the voice client to
    # hold the wake/command window open this many more seconds, refreshed per command, so a follow-up
    # ("skip", "louder", "pause") needs no re-wake while music plays — the wake-gate otherwise idle-closes
    # the window (~15s) and silently locks the user out mid-interaction. Tunable: raise if follow-ups still
    # get gated, lower if open-mic asides leak (a hot mic over music transcribes undirected speech as
    # asides). 0 disables. The voice side caps it with its own max-hold ceiling so a missed refresh
    # self-heals. Env: GABAI_MEDIA_KEEPALIVE_SECS.
    media_keepalive_secs: int = 30
    # The sink-input % a freshly-played Jellyfin movie should start at. 100 = neutral (no per-stream
    # attenuation; the device/player volume governs actual loudness) — NOT "loud". This un-strands the
    # movie-starts-quiet bug: PipeWire's stream-restore replays the PRIOR movie's ducked/low level (e.g. 18%,
    # the duck floor) onto the new stream, and resetting <video>.volume can't reach that sink layer. Lower it
    # (e.g. 80) to make movies start gentler — a fresh-start baseline, never carried over from the last movie.
    # Applied on play only (resume keeps the level you set); only raises a stranded-LOW sink, never lowers one.
    # Clamped to [20, 100] so a bad value can't strand a movie inaudible. Env: GABAI_MOVIE_START_VOLUME.
    movie_start_volume: int = 100
    # Duck watchdog: a brain-side safety net against a stuck duck — if media stays ducked this many seconds
    # with NO voice activity to refresh it (no /media/duck on, no incoming utterance), auto-restore. Catches a
    # brain↔voice desync where the voice side opens a duck (e.g. for a movie-title announcement) but never
    # sends the matching off, stranding the movie at the duck floor in silence. Refreshed by every duck-on AND
    # every incoming voice turn, so it never fires during a legitimate sustained hold (dictation keeps it
    # alive). Ticks on the existing ~1 Hz /media/state poll — no background task. 0 disables. Keep it above the
    # longest bot announcement so a long title read isn't cut. Env: GABAI_DUCK_WATCHDOG_SECS.
    duck_watchdog_secs: int = 12
    # This machine's friendly name, used to tag media sources as LOCAL vs on another device/room (e.g.
    # "EndeavorMain"). Empty → defaults to the hostname at use. The brain only AUTO-ducks/controls media it
    # judges local to this device; remote sources are visible (for future explicit control) but never touched
    # automatically. Env: GABAI_LOCAL_DEVICE.
    local_device: str = ""
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    attestation: AttestationConfig = Field(default_factory=AttestationConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    tidal: TidalConfig = Field(default_factory=TidalConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
