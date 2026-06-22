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
    # Emit Aria "HAL eye" status (thinking/idle/off) to the state file during TUI turns so a Conky
    # eye panel glows for typed sessions too. Opt-in; the voice front-end is the writer in voice mode.
    aria_eye: bool = False
    # Voice mode (gab --voice-serve). Empty voice_model ⇒ normal router (arya base,
    # escalate to Claude); a non-empty voice_model pins that single model.
    voice_model: str = ""
    voice_port: int = 8765
    # Bind address for the voice-brain HTTP+SSE server. Default 127.0.0.1 = loopback-only (the brain and
    # the voice front-end share a host, the historical assumption). For a remote satellite (a thin voice
    # box on the LAN talking to this brain — Pi Topology B), bind a SPECIFIC host IP (e.g. the EM LAN
    # address "192.168.1.155"), NOT 0.0.0.0 — a specific bind narrows the exposed surface to one interface.
    # Pair a non-loopback bind with `voice_auth_token` (a LAN-reachable /respond is a remote command surface).
    # CLI --voice-host overrides this. Env: GABAI_VOICE_HOST.
    voice_host: str = "127.0.0.1"
    # Optional shared-secret bearer token for the voice-brain endpoints. Empty (default) = no auth, correct
    # for a loopback bind. When set, every endpoint except /health requires `Authorization: Bearer <token>`
    # (constant-time compared); a missing/wrong token gets 401. REQUIRED in practice whenever `voice_host`
    # is non-loopback, so a LAN-exposed /respond can't be driven by anything but the paired satellite (which
    # carries the same token in its .env). Env: GABAI_VOICE_AUTH_TOKEN.
    voice_auth_token: str = ""
    voice_safe_zones: list[str] = Field(default_factory=list)
    voice_passphrase: str = ""
    voice_persona: str = ""
    # Self-learning persona layer: a GLOBAL (one-Aria), user-invisible personality that grows over
    # sessions. A reflection pass at brain shutdown reads the session under 5 rails and consolidates
    # an always-injected, bounded INDEX of traits. `voice_persona` above is the legacy static fallback
    # used only when this is disabled/empty. Stored at data_dir()/persona/ (cwd-independent).
    persona_enabled: bool = True
    persona_reflect_on_shutdown: bool = True
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
    # Conversation-hold release: on a TERMINAL one-shot reply (a self-contained answer with no expected
    # follow-up — not a media-control turn, not a reply ending in a question), emit a `convo_hold` event
    # so the voice side drops the bed-duck immediately instead of holding it the full conversation-hold
    # window after an addressed reply over playing media. Pure optimization — a missed/absent event
    # degrades to the voice-side timed hold, so it's emitted conservatively. False disables the hint.
    # Env: GABAI_VOICE_CONVO_HOLD_RELEASE.
    voice_convo_hold_release: bool = True
    # Voice-volume control (F3): when the user asks to change ARIA'S OWN speaking volume ('lower your
    # voice', 'speak up'), the voice.set_volume command records a per-turn signal and the turn emits a
    # `voice_volume` SSE event the voice side maps onto its TTS gain. Distinct from media/system volume.
    # False disables the emit (the command still speaks its confirm but no event crosses). Kill-switch.
    # Env: GABAI_VOICE_VOLUME_CONTROL.
    voice_volume_control: bool = True
    # Item C — STT wake-expansion guard. A bare wake ("Hey Aria", ~0.7s) can be fluently mis-transcribed
    # into a question ("Hey, how are you?"), defeating the text-only listen-first guard (no wake token
    # survives the rewrite; "how" even fast-passes as a question word). The voice side carries the
    # acoustic fact out of band on /respond: a nested `wake` object whose `bare_wake_likelihood` (fused
    # voice-side, duration-dominant — duration being evidence the brain can't see) says how likely the
    # utterance is NOTHING but the wake. When it's >= the threshold, is_addressed suppresses the
    # zero-latency fast-pass and routes to the one-shot LLM classify with a wake-context hint, so a
    # content-free pleasantry resolves to wake-only/silence. The signal can NEVER directly suppress a turn
    # (it only ever demotes a fast-pass to the careful classify), so a wrong likelihood costs at most one
    # cheap classify on a real command the LLM still answers — the addressed.py "never eat a command"
    # invariant holds. Absent `wake` => exact current behavior (arrival-keyed, safe ahead of the producer).
    # False disables the whole consumer. Env: GABAI_VOICE_WAKE_CONFIDENCE_FILTER.
    voice_wake_confidence_filter: bool = True
    # Suppression floor for `bare_wake_likelihood` (0..1). Co-tuned against dlog receipts in a joint drive;
    # the voice-side scale is monotonic so this stays meaningful. Env: GABAI_VOICE_BARE_WAKE_THRESHOLD.
    voice_bare_wake_threshold: float = 0.8
    # Internet-outage failover: when a voice turn's cloud LLM call fails with a CONNECTIVITY error (no
    # network / DNS failure / connect timeout — NOT a 4xx/5xx, which means the server answered), auto-fail
    # the turn over to the on-demand local model (`local_model`) so Aria still answers offline, and probe
    # the cloud at the top of each subsequent turn to switch back when it returns. Requires `local_model`
    # set (else there's nothing to fall to and this is a no-op). A spoken notice marks each transition.
    # False disables the whole auto-failover (a manual "switch to local" still works). Env:
    # GABAI_VOICE_OFFLINE_FAILOVER.
    voice_offline_failover: bool = True
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
    # alive — and during a long bot REPLY, the voice side refreshes it ~1 Hz via bot_speaking=true on the poll,
    # so a story can't trip it mid-narration; see voice/server.py). Ticks on the existing ~1 Hz /media/state poll
    # — no background task. 0 disables.
    # ★ COUPLED INVARIANT: duck_watchdog_secs (20) MUST stay > the voice-side conversation-hold window (~15s).
    # After BotStopped the bot_speaking refresh stops, so the watchdog grace re-bases to ≈BotStopped and then runs
    # for `duck_watchdog_secs`; the voice single-writer restores the bed at BotStopped + convo-hold (~15s). The 5s
    # margin (20 > 15) is what lets the voice restore win — if the watchdog ever drops below the convo-hold, it
    # fires first and pops the bed ~early AND emits an off media_duck didn't ask for. So if the voice convo-hold is
    # ever raised, raise this in step (keep the margin). With the single-writer fix a genuine stuck duck is rare, so
    # a looser backstop costs little — and the crash-net stays honest (no TTS → no refresh → fires on real silence).
    # Env: GABAI_DUCK_WATCHDOG_SECS.
    duck_watchdog_secs: int = 20
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
