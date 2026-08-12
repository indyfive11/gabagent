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
    # GLOBAL Jellyfin cast target (the native-client DeviceName to PlayNow+control via REST, e.g. a Jellyfin
    # Media Player / mpv-shim). Empty (default) ⇒ no global target ⇒ the owned-browser play path (host/KDE
    # behavior, byte-identical). This is the SINGLE-ROOM analog of room_media[<room>].jellyfin_client_target:
    # a single-room install whose desktop is NOT KDE (the Cinnamon laptop — the owned-browser path's KWin
    # fullscreen can't run there) sets this so play() casts to a native client instead, with no room_media /
    # room_id plumbing. Precedence: per-room target wins, else this global. Env: GABAI_JELLYFIN__CLIENT_TARGET.
    client_target: str = ""
    # A PipeWire sink-input substring the brain's universal full-mute local-duck (voice/ducking.py
    # _duck_local_sinks) must NOT touch, because a satellite-side gentle-duck belt already owns that node.
    # Set to the cast movie node (e.g. the laptop JMP/mpv `node.name`) so the brain doesn't ALSO hard-mute it
    # on window-open and race the belt on restore. Empty (default) ⇒ no extra exclusion ⇒ host/satellite byte-identical.
    # Env: GABAI_JELLYFIN__CAST_DUCK_EXCLUDE_MATCH.
    cast_duck_exclude_match: str = ""


class RadarrConfig(BaseModel):
    """Radarr integration — voice "add a movie to download" (first-party provider).

    A single household downloader (no per-room dimension). base_url/api_key live in settings.json only
    (installation-specific secret; nested-env is intentionally NOT wired, so there is no GABAI_RADARR__*).
    Unconfigured (empty api_key) ⇒ the provider never surfaces (detect() returns False) ⇒ an install with
    no Radarr behaves exactly as before. Every install-specific value is a config field with a safe default:
    an ambiguous instance (multiple root folders / quality profiles) is refused with spoken guidance rather
    than silently guessing (auto-pick only when exactly one exists) — see providers/_arr.py."""
    enabled: bool = True
    base_url: str = "http://localhost:7878"
    api_key: str = ""                     # Radarr → Settings → General → API Key
    # Quality profile to add under, matched by NAME (portable across installs; ids are install-local).
    # Empty ⇒ auto-pick only if the instance has exactly one profile, else refuse (Radarr ships several
    # defaults incl. "Any", and auto-picking the first can grab any-quality releases — so require a choice).
    quality_profile: str = ""
    # Root download folder PATH. Empty ⇒ auto-pick only if exactly one accessible root exists, else refuse
    # (a wrong pick sends a multi-GB download to the wrong/offline disk).
    root_folder_path: str = ""
    minimum_availability: str = "released"   # MovieStatusType: tba|announced|inCinemas|released|deleted
    monitor: str = "movieOnly"               # MonitorTypes: movieOnly|movieAndCollection|none
    search_on_add: bool = True               # kick off a search immediately after adding


class SonarrConfig(BaseModel):
    """Sonarr integration — voice "add a show to download" (first-party provider). Sibling of RadarrConfig
    (same /api/v3 + X-Api-Key shape); see it for the config/secret/ambiguity philosophy. Series carry
    seasons, so `add` echoes the looked-up seasons and monitors per the `monitor` mode. languageProfileId is
    resolved at runtime (present on Sonarr v3, gone on v4) — never a config-hardcoded id."""
    enabled: bool = True
    base_url: str = "http://localhost:8989"
    api_key: str = ""                     # Sonarr → Settings → General → API Key
    quality_profile: str = ""             # by name; empty ⇒ auto-if-one-else-refuse (see RadarrConfig)
    root_folder_path: str = ""            # empty ⇒ auto-if-one-else-refuse
    season_folder: bool = True            # organise episodes into per-season folders
    monitor: str = "all"                  # Sonarr MonitorTypes: all|future|missing|existing|firstSeason|latestSeason|pilot|none
    search_on_add: bool = True


class TmdbConfig(BaseModel):
    """The Movie Database — the first (default) discovery source for MovieScout (voice "suggest N good
    movies to download"). A read-only credential for TMDB's public v3 API; installation-specific secret,
    settings.json only (nested-env deliberately unwired, mirrors RadarrConfig — no GABAI_TMDB__*). Empty
    api_key ⇒ MovieScout never surfaces (detect() is config-only, so host/satellite stay byte-identical). Free key
    from themoviedb.org → Settings → API. This is the SOURCE credential; recommender policy lives in
    MoviescoutConfig so a second source (Trakt) slots in with its own credential block and zero policy
    duplication."""
    api_key: str = ""
    lang: str = "en-US"   # TMDB result language/region for titles + overviews


class MoviescoutConfig(BaseModel):
    """MovieScout recommender policy — source-agnostic (the discovery source is configured separately via
    its own credential block, e.g. TmdbConfig). Every value is a plain, user-editable field with a safe
    default; nothing here is install-specific. The only cache is the expensive discovery map (neighbor
    lists per owned movie), TTL'd below — the owned metadata itself is already fast from one GET
    /api/v3/movie, so there is deliberately no metadata cache."""
    enabled: bool = True
    recs_ttl_days: int = 45          # a seed's cached neighbor list is refreshed after this many days
    offered_cooldown_days: int = 21  # don't re-offer a title suggested within this window (re-offer after)
    seed_count: int = 12             # owned movies sampled per ask (genre-proportional) to expand from


class TidalConfig(BaseModel):
    """TIDAL via a local Mopidy + mopidy-tidal server (first-party provider).

    Mopidy exposes an HTTP JSON-RPC API; this skill drives search → queue → play and
    transport over it. See the setup note for installing/authorizing mopidy-tidal.
    """
    enabled: bool = True
    rpc_url: str = "http://localhost:6680/mopidy/rpc"
    rpc_timeout: float = 0.0   # per-call RPC timeout (s); 0 ⇒ the module default (_RPC_TIMEOUT, 30s)


class RoomMediaProfile(BaseModel):
    """Per-room media target overrides (Phase 10 / #62). Each field overrides the corresponding GLOBAL
    provider endpoint for ONE room (keyed by room_id in GabAgentConfig.room_media). Empty ⇒ fall through
    to the global provider config, so a room with no profile — and any unconfigured install — behaves
    EXACTLY as today (byte-identical). Example:
        room_media = {"living_room": {"tidal_rpc_url": "http://192.0.2.50:6680/mopidy/rpc"}}
    routes that room's music control AND its RPC duck to the Pi's Mopidy instead of the brain-host's."""
    tidal_rpc_url: str = ""   # override TidalConfig.rpc_url for this room's Mopidy endpoint (unset ⇒ global)
    tidal_rpc_timeout: float = 0.0   # override TidalConfig.rpc_timeout for this room (s); 0 ⇒ global default
    # This room ducks its own music locally at the sink (satellite-side PipeWire belt), so the brain must
    # NOT also duck via the Mopidy mixer-RPC. On a satellite whose Mopidy software-mixer can't be reliably
    # ducked over RPC (e.g. the Pi — value changes but output doesn't, or a phantom-0 right after play), the
    # brain's mixer duck is at best a no-op that mis-reports `ducked:["tidal"]` and at worst saves a 0 prior
    # that restores the music to silence. True ⇒ the brain skips its tidal duck for this room and the
    # satellite owns it. Default false ⇒ the brain ducks as before (host/global byte-identical).
    duck_local: bool = False
    # Per-room Jellyfin (Phase 10 / #62 video half). In THIS deployment both rooms share the ONE home
    # Jellyfin server; a room is distinguished only by WHICH session it casts to — so the active per-room
    # override is `jellyfin_client_target` (the room's native client, e.g. the Pi's mpv-shim, which streams
    # from the shared home server and plays on the room's own screen instead of the brain-host's browser).
    jellyfin_client_target: str = ""   # Jellyfin DeviceName to cast+control (the room's mpv-shim client)
    # base_url/api_key/user_id are an OPTIONAL future multi-server hook (mirrors the Tidal per-room
    # override) — UNUSED in this single-server deployment: leave empty ⇒ fall through to the shared global
    # home Jellyfin (byte-identical). Only set these if a room ever points at a DIFFERENT Jellyfin server.
    jellyfin_base_url: str = ""        # override the room's Jellyfin server URL (empty ⇒ shared global)
    jellyfin_api_key: str = ""         # per-room API key (empty ⇒ global key)
    jellyfin_user_id: str = ""         # per-room user id for played/unwatched filters (empty ⇒ global)


class TmiConfig(BaseModel):
    """Tiered-Memory-Indexing: a self-reconciling, cross-room memory layer.
    Tier 0 = shared across ALL Aria processes (the 'one Aria' durable facts), Tier 1 = per-room,
    Tier 2 = the recent message window (the existing session recency slice, not a new store). An
    auto-reconciler (riding the persona shutdown trigger) consolidates/prunes per room and escalates
    only HIGH-weight facts up into Tier 0 under guardrails. DEFAULT OFF ⇒ exact current persona/memory
    behavior (a single shared persona); nothing here changes a turn until `enabled` is set."""
    enabled: bool = False  # master switch; off = byte-identical to today
    # Tier-0 escalation (P3): promote high-signal Tier-1 facts into the shared cross-room store. Off ⇒
    # no Tier-0 fact store is written and recall stays persona-only, exactly as P1/P2 behave.
    tier0_escalation_enabled: bool = False
    # Tier-0 adaptive pressure-banded cap. soft_cap = where back-pressure begins (the admission bar
    # rises and pruning hardens per band: relaxed 0-19 / medium 20-29 / firm 30-39 / strict 40-49);
    # hard_cap = absolute ceiling, no net growth past it (must prune below it before a new fact lands).
    # The band curve (admit multipliers, decay half-lives, prune floors) is an internal default, tuned
    # later on real logs. Pinned/explicit facts are exempt from decay+prune.
    tier0_soft_cap: int = 20
    tier0_hard_cap: int = 50
    # Habit escalation: a fact graduates to Tier 0 when seen >= habit_count times spanning at least
    # habit_window_days AND >= 2 distinct weeks (persistence, not a one-week spike). Tunable.
    habit_count: int = 3
    habit_window_days: int = 14
    # Weighted decay: an unused (un-pinned, non-explicit) fact's weight halves every N days and ages out,
    # so Tier 0 self-corrects a bad escalation and stays 'what's true lately'.
    decay_halflife_days: int = 30
    # Privacy switch: rooms whose memories NEVER escalate to shared Tier 0. Empty = allow-all (matches
    # today's single shared persona). Add a room_id here to keep that room's memory private to itself.
    tier0_exclude_rooms: list[str] = Field(default_factory=list)
    # Auto-reconcile cadence for a long-lived room process. 0 = off (v1 reconciles only on the shutdown
    # trigger, like persona reflection). A periodic in-process reconcile is a later phase.
    reconcile_interval_secs: int = 0


class StratumConfig(BaseModel):
    """Stratum — native memory-management subsystem (docs/STRATUM.md). Three thin additions to the
    existing per-cwd memory.md: a Current Focus window, a Prep-for-Compact routine, and a subordinate
    Observed-Habits store. DEFAULT OFF ⇒ byte-identical to today: every lifecycle seam is gated on
    `enabled`, the store is created lazily (no new files while off), and the tools register only when
    enabled. Coding-lane only (never the voice runner) and gated OFF inside sub-agents."""
    enabled: bool = False  # master switch; off = byte-identical to today
    # "reviewed" (default): at compact-prep, proposed habit accretions pass through ONE adversarial
    # reviewer (the user's proxy) that vets them against scope + existing memory before they land — the
    # reviewer fires ONLY when there are habits to judge, so the common Current-Focus-only compaction
    # stays a single call. "auto": skip the reviewer, trust the deterministic bound (cheapest). Either
    # way habits are subordinate and promotion to a durable rule is always user-gated.
    observation_mode: str = "reviewed"
    # compact-prep runs at the top of _compact_context (before the summary); this ratio is documentary
    # (the trigger is the existing 0.85 compaction path). Kept for future use; must stay < 0.85.
    compact_prep_ratio: float = 0.70
    # Tier-0.5 scoring/decay + caps + eligibility gate (Stratum §7.4-§7.6). Defaults are conservative.
    tier05_halflife_days: int = 30
    tier05_soft_cap: int = 40
    tier05_hard_cap: int = 75
    adv_days: int = 30
    adv_hits: int = 5
    adv_weeks: int = 3
    # Current Focus / whole-memory.md LINE budgets (never bytes) for the size reminder.
    cf_notice: int = 150
    cf_firm: int = 300
    cf_strict: int = 600
    idx_notice: int = 400
    idx_firm: int = 700
    idx_strict: int = 1000
    # Staleness audit horizon (observed-store last_seen); light. Identical (tool,args) N times → signal.
    audit_threshold_days: int = 90
    repeat_signal_threshold: int = 3
    # Same-file re-edit count that emits an edit-churn signal (deterministic indecision marker).
    edit_churn_threshold: int = 3
    # Optional model override for the out-of-band compact-prep + reviewer calls ("" = inherit self.model).
    # These are off-live-path judgment calls, so a cheaper/faster model is usually right.
    model: str = ""
    # Deterministic "nothing drastic" bound on a Current Focus rewrite: reject (keep the old window) if
    # the rewrite drops more than this fraction of the old block's non-blank lines, or drops a Blocked
    # line the old block had. Guards memory files without an LLM.
    cf_max_line_drop_frac: float = 0.5
    # Retention for the compact-prep memory snapshots (`*.pre-stratum-*`): keep the newest N per file.
    snapshot_keep: int = 5


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


class ImageGenConfig(BaseModel):
    """Image generation via the Gab/Aria `/v1/images/generations` endpoint (first-party provider).

    The `generate_image` tool calls the endpoint, downloads the (public CDN) PNG to a local file under
    the output dir, and produces a display descriptor {path,url,mime,w,h,id,ttl_secs} for the future voice
    display seam. GA owns generation + local-file GC; the returned CDN url is PUBLIC, so no brain-side file
    server is needed for cross-host satellites — they fetch the url directly. Default-on but inert until the
    `generate_image` tool is actually invoked, so an unconfigured install behaves exactly as before."""
    enabled: bool = True
    # Default model for a plain "generate an image of X" (overridable per-call by the tool's `model` arg).
    # Any /v1/models image-capable id. gpt-image-1 = flagship quality (~5 credits); gpt-image-2 / gpt-image-1-
    # mini / image-generator are cheaper. The authoritative per-call charge is the response's usage.credits_used
    # (the catalog base_cost is only a floor). Env: GABAI_IMAGE__MODEL.
    model: str = "gpt-image-1"
    # Default size ("WxH"). Empty ⇒ let the endpoint pick its own default. Env: GABAI_IMAGE__SIZE.
    size: str = "1024x1024"
    # Where generated PNGs are written. Empty (default) ⇒ data_dir()/images. A concrete path travels to any
    # host. Env: GABAI_IMAGE__OUTPUT_DIR.
    output_dir: str = ""
    # Local-file GC: on each generation, delete files in the output dir older than this many seconds (GA owns
    # cleanup per the image-seam contract; the CDN copy is Gab's to retain, not ours). 0 disables GC. 24h default.
    # Env: GABAI_IMAGE__TTL_SECS.
    ttl_secs: int = 86400
    # Per-call request timeout (s). Image generation can take 10-30s. Env: GABAI_IMAGE__TIMEOUT_SECS.
    timeout_secs: float = 120.0


class GabAgentConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GABAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # An empty GABAI_* env var (exported but "") is treated as UNSET, for every field — so an
        # accidentally-blank export can't override a configured file/default value. This closes two live
        # footguns under the env-over-file precedence: a blank GABAI_VOICE_ADVERTISE= silently flipping
        # discovery off, and a blank GABAI_VOICE_AUTH_TOKEN=/GABAI_API_KEY= silently disabling a configured
        # secret. Side effect: a blank numeric env (e.g. GABAI_VOICE_PORT=) degrades to unset→default rather
        # than a ValidationError. The only thing lost is "force a field to empty via env over a non-empty
        # file value" — exotic and recoverable by editing the file. Applies to env AND dotenv sources.
        env_ignore_empty=True,
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
    stratum: StratumConfig = Field(default_factory=StratumConfig)
    searxng_url: str = ""
    vim_mode: bool = False
    theme: str = "monokai"
    load_global_claude_md: bool = False
    local_base_url: str = "http://localhost:11434/v1"
    local_model: str = ""
    # Environment overlay for the spawned local-model server (`ollama serve`). Default empty ⇒
    # nothing injected, which is the universal-safe behavior: a generic install gets Ollama's own
    # defaults, never a GPU override that fits only one card. A ROCm box whose arch needs spoofing
    # sets e.g. {"HSA_OVERRIDE_GFX_VERSION": "11.0.0"} here — detected once and written by the Local
    # backend setup (from rocminfo), then user-editable. Replaces a former hard-coded HSA_OVERRIDE.
    local_env: dict[str, str] = Field(default_factory=dict)
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
    # box on the LAN talking to this brain — Pi Topology B), bind a SPECIFIC host IP (e.g. the brain's LAN
    # address "192.0.2.10"), NOT 0.0.0.0 — a specific bind narrows the exposed surface to one interface.
    # Pair a non-loopback bind with `voice_auth_token` (a LAN-reachable /respond is a remote command surface).
    # CLI --voice-host overrides this. Env: GABAI_VOICE_HOST.
    voice_host: str = "127.0.0.1"
    # Optional shared-secret bearer token for the voice-brain endpoints. Empty (default) = no auth, correct
    # for a loopback bind. When set, every endpoint except /health requires `Authorization: Bearer <token>`
    # (constant-time compared); a missing/wrong token gets 401. REQUIRED in practice whenever `voice_host`
    # is non-loopback, so a LAN-exposed /respond can't be driven by anything but the paired satellite (which
    # carries the same token in its .env). Env: GABAI_VOICE_AUTH_TOKEN.
    voice_auth_token: str = ""
    # Token pairing (`gab pairvoiceagent`): how long the operator-opened pairing WINDOW stays open for a new
    # front end to register (seconds), and how long an ACCEPTED candidate stays retrievable — its CLAIM TTL —
    # so a dropped response can recover. Defaults (5 min / 30 s) suit a hands-on install; both are safe to
    # leave unset (the getattr in the server mirrors these). Env: GABAI_VOICE_PAIR_WINDOW_SECS / _CLAIM_SECS.
    voice_pair_window_secs: float = 300.0
    voice_pair_claim_secs: float = 30.0
    # Advertise the brain on the LAN via mDNS/DNS-SD (_voice-brain._tcp) so a satellite can discover the
    # host without a hand-typed IP. Default False = the historical no-op: an install that never opted in
    # broadcasts nothing (a LAN brain announcing its presence is a deliberate choice, not a side effect of
    # upgrading). The voice-host installer role WRITES this True when it provisions a LAN brain; a manual
    # install sets it here or via GABAI_VOICE_ADVERTISE. A loopback `voice_host` is never advertised even
    # when True (nothing to discover). Fail-soft: absent `zeroconf` (optional dep) degrades to no
    # advertisement, never a crash. Discovery is a PARALLEL enhancement over a hand-configured host.
    voice_advertise: bool = False
    # This brain's own room identity, published in the mDNS TXT (`room_id`) so a multi-room satellite can
    # filter its browse to the RIGHT brain. Empty (default) = a single-room install: the advertiser still
    # publishes an empty `room_id`, which a satellite reads as "the default/only brain" — unchanged behavior.
    # Env: GABAI_VOICE_ROOM_ID.
    voice_room_id: str = ""
    voice_safe_zones: list[str] = Field(default_factory=list)
    # Builder auto-run guardrail: directory roots inside which `send_to_builder` may dispatch WITHOUT a
    # keyboard confirm (Tier 1). A target outside every root stays keyboard-gated (fail-safe). Empty
    # (default) ⇒ no folder auto-approved ⇒ unchanged confirm-everywhere behavior. The user graduates a
    # folder into this list once they trust the build there. Env: GABAI_BUILDER_ALLOWED_ROOTS.
    builder_allowed_roots: list[str] = Field(default_factory=list)
    # Builder sandbox: parent dir for throwaway/new builder projects ("new builder project called X" →
    # <root>/X). A path-less `send_to_builder` lands here. Empty (default) ⇒ no sandbox; a path-less
    # dispatch falls back to the cwd (legacy). Set to e.g. ~/builder. Env: GABAI_BUILDER_SCRATCH_ROOT.
    builder_scratch_root: str = ""
    # Builder graduation target: parent dir a matured project is promoted into ("graduate it as X" →
    # <root>/X), after which <root>/X is appended to builder_allowed_roots. Empty (default) ⇒ graduation
    # is unavailable until configured. Set to e.g. ~/dev. Env: GABAI_BUILDER_GRADUATE_ROOT.
    builder_graduate_root: str = ""
    # Closed-set fuzzy-salvage strictness — how confidently a garbled command id / playlist name is
    # auto-resolved against the real catalog before we ask "did you mean …?" instead. Defaults are the
    # historical bare constants (resolve.py 0.6/0.86, tidal 0.72) ⇒ an unconfigured install behaves
    # EXACTLY as before. A process fed by a WEAKER STT (the fat-thin `mobile` laptop client on local
    # small.en, which garbles more than cloud arya) RAISES these so a low/mid-confidence match becomes a
    # safe "ask" rather than a confident WRONG-salvage (the dangerous misfire on a command terminal — see
    # the fat-thin design R7). Higher = stricter = fewer confident-wrong, more clarify prompts. Because the
    # `mobile` brain is its own process (process-per-room), "stricter mobile" is just its own config —
    # no per-room branching. Env: GABAI_SALVAGE_COMMAND_CUTOFF / _AUTO_ROUTE_RATIO / _PLAYLIST_PLAY_SCORE.
    salvage_command_cutoff: float = 0.6        # difflib floor for a near-match to even be a candidate
    salvage_auto_route_ratio: float = 0.86     # single best near-match this close ⇒ auto-route (else suggest)
    salvage_playlist_play_score: float = 0.72  # explicit playlist match this strong ⇒ auto-play (else ask)
    voice_passphrase: str = ""
    voice_persona: str = ""
    # Self-learning persona layer: a GLOBAL (one-Aria), user-invisible personality that grows over
    # sessions. A reflection pass at brain shutdown reads the session under 5 rails and consolidates
    # an always-injected, bounded INDEX of traits. `voice_persona` above is the legacy static fallback
    # used only when this is disabled/empty. Stored at data_dir()/persona/ (cwd-independent).
    persona_enabled: bool = True
    persona_reflect_on_shutdown: bool = True
    # Self-knowledge introspection: on a voice turn whose user question is an explanatory self-question
    # ("how do you pick a model", "what happens if the internet drops", "what are your limits"), inject a
    # curated "how I work" doc into the system prompt so Aria explains herself in one turn. Gated on
    # intent → zero cost on normal/command turns. Default-on (gated + benign); set false to disable.
    introspect_enabled: bool = True
    # Validate the assembled router ladder's gab-backend rungs against the live model catalog
    # (cached by `gab --models`). Default-on but a NO-OP until a cache exists, so an unconfigured
    # install behaves exactly as before; set false to skip validation even when a cache is present.
    models_catalog_validate: bool = True
    # Low-balance guard for credit-spending tools (image-gen, video). When > 0, a spend tool adds a brief
    # heads-up ("you're low on credits — N left") once the spendable pool (GET /v1/credits total_available)
    # falls below this many credits; the check reads a short-TTL cache, never polling per call. 0 (default)
    # disables the guard entirely — an unconfigured install behaves exactly as before. `gab --credits` shows
    # the live balance and this guard's state. Env: GABAI_CREDITS_LOW_THRESHOLD.
    credits_low_threshold: int = 0
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
    # Bare-direction guard: a lone ambiguous direction word ('up'/'down'/'on'/'off') with nothing else is
    # meaningless without a verb ('turn it up') and is a classic garbled-STT fragment — yet the LLM will
    # happily classify it into a state-changing command (live 2026-06-23, Pi: a fragmented utterance
    # arrived as bare 'up' → auto-ran system.volume_up on a turn the user never asked for). When the
    # addressed utterance is exactly such a token, ask instead of acting. Explicit terse commands
    # ('stop'/'pause'/'skip'/'next'/'mute'/'louder') are NOT bare-direction tokens and pass through.
    # Env: GABAI_VOICE_BARE_DIRECTION_GUARD.
    voice_bare_direction_guard: bool = True
    commands_enabled: bool = True  # voice command framework (capability discovery + run_command)
    # Hold playing music at this % cap continuously (not just on speech) so VAD can hear the user over
    # it — the speech-duck drops it deeper, then restores to this cap. 100 disables. Slide down if VAD
    # tuning alone can't keep up. Env: GABAI_MEDIA_AMBIENT_CAP.
    media_ambient_cap: int = 90
    # Floor for an absolute media-volume set (tidal.set_volume): clamp the requested level up to at
    # least this % so "turn the music way down" can't ratchet to inaudible — to actually silence music
    # the user pauses/stops it. Env: GABAI_MEDIA_VOLUME_FLOOR.
    media_volume_floor: int = 5
    # Step size (percentage points) for a RELATIVE music-volume change (tidal.adjust_volume — "turn it up/
    # down", "louder", "quieter"). Bumped from the current level so a relative request is always audibly
    # different, instead of the model guessing an absolute target. Env: GABAI_MEDIA_VOLUME_STEP.
    media_volume_step: int = 15
    # Latency-gated progress-ack: if a turn produces no speech within this many ms, Aria emits ONE short
    # filler ("One moment.") so a slow think or a long command (e.g. a 9s Tidal search) doesn't feel like a
    # dead stick — the user knows she heard them. Fast turns (speech before the threshold) never trigger it,
    # so terseness survives. Domain-aware reassurance ("Trying Tidal…") is handled separately once the tool
    # is known; this covers the PRE-decision silence that can't. DEFAULT 0 = OFF (the historical no-op, per
    # the config-generalization SOP — it adds a NEW spoken utterance, so it's opt-in + live-verified like the
    # other speak/spend features; set e.g. 2200 to enable). Env: GABAI_VOICE_PROGRESS_ACK_MS.
    voice_progress_ack_ms: int = 0
    # The phrase spoken by the latency-gated progress-ack (above). Kept short and non-committal since the
    # command isn't known yet. Env: GABAI_VOICE_PROGRESS_ACK_PHRASE.
    voice_progress_ack_phrase: str = "One moment."
    # Cold-start pre-warm (#2): when True, the /prewarm endpoint fires a throwaway arya completion on the
    # voice side's first-post-wake-voice-energy trigger, so the cloud session is warm by the time the real
    # turn arrives (overlapping arya's deep-cold spin with the user's own speaking time). False → endpoint
    # no-ops (kill-switch). Inert unless the voice client calls it. Env: GABAI_VOICE_PREWARM_ENABLED.
    voice_prewarm_enabled: bool = True
    # Per-room cooldown (seconds) between pre-warm completions, so a chatty onset or a double-fire can't
    # spend repeated arya calls. Env: GABAI_VOICE_PREWARM_COOLDOWN_SECS.
    voice_prewarm_cooldown_secs: float = 4.0
    # Cross-room wake arbiter (Stage 2 of the double-answer fix) — the brain-side first-to-hear referee that
    # rides on /prewarm. OFF by default: unset ⇒ /prewarm is warm-only and behavior is byte-identical to
    # today (Stage 1 threshold-zoning ships first; this is the door-open fallback, flipped on only if the
    # gross acoustic separation isn't enough). When on, a /prewarm carrying a `wake_claim` opens/joins a
    # short grace window and returns a proceed|stand_down verdict the voice side honors before burning STT.
    # Arbitration is host-disk-local (an flock'd window file) → a cross-host brain physically can't touch it,
    # so it never participates and any solo/remote install stays byte-identical. Env: GABAI_VOICE_WAKE_ARBITER_ENABLED.
    voice_wake_arbiter_enabled: bool = False
    # Grace window (seconds) a wake claim is held to collect near-simultaneous peers before the winner is
    # picked by earliest NORMALIZED receipt (server-side host-clock arrival minus each device's calibrated
    # detector latency). Must cover hall-leak acoustic delay + the calibrated detector delta and NO wider —
    # a wider window raises the one worse-than-today case (two DISTINCT simultaneous utterances false-merged).
    # ~0.25s is enough for a same-house tens-of-ms acoustic delta. Env: GABAI_VOICE_WAKE_ARBITER_WINDOW_SECS.
    voice_wake_arbiter_window_secs: float = 0.25
    # Never-zero fallback: how long a stood-down room waits before probing whether the winner took the turn,
    # then un-stands-down if it didn't — so the worst case is today's double answer, never a zero answer. The
    # winner stamps `committed` the instant it accepts `proceed` (BEFORE STT, lands in ms), so this only needs
    # to cover claim→verdict→commit round-trip, NOT the 6-26s a real turn takes to reach /respond. Env:
    # GABAI_VOICE_WAKE_ARBITER_RESOLVE_SECS.
    voice_wake_arbiter_resolve_secs: float = 1.0
    # Liveness grace for the commit mark. 0 ⇒ PRESENCE-based: any commit means the winner started → the peer
    # stays down (a winner that started then died mid-turn is a single-device failure, out of arbiter scope).
    # >0 ⇒ HEARTBEAT mode: the winner must refresh `committed` within this grace or the peer un-stands-down
    # (covers mid-turn death); set it comfortably above the voice-side heartbeat interval to tolerate a missed
    # beat. Default 0 (presence) — the simplest correct fix for the double-answer. Env: GABAI_VOICE_WAKE_ARBITER_LIVENESS_SECS.
    voice_wake_arbiter_liveness_secs: float = 0.0
    # Latency self-test → auto-Turbo (#6). OFF by default (Rob's call) — it auto-spends (Turbo billing +
    # recovery probes), so it stays opt-in: flip voice_latency_watch on for a bad-cloud day. When on, the
    # brain passively watches real arya ttft and, when arya is the DOMINANT slowness, offers to switch
    # command turns to the fast (Claude) rung; "yes" toggles Turbo. All values below are live-tunable.
    voice_latency_watch: bool = False
    # Trip when the median of the last `window` arya-DOMINATED turns exceeds this, OR any single
    # arya-dominated turn exceeds hard_ceiling. From last night's data (warm ~3s, cold 18-26s).
    voice_latency_ceiling_ms: int = 8000
    voice_latency_hard_ceiling_ms: int = 15000
    voice_latency_window: int = 3
    # Consider arya "recovered" once the last `window` samples are all below this (hysteresis vs ceiling).
    voice_latency_floor_ms: int = 4000
    # Arya counts as the DOMINANT cause of a slow turn only when its ttft is at least this fraction of the
    # turn's total time — so a turn that felt slow because of a long Tidal/command call (which Turbo can't
    # speed) does NOT trigger the offer (VAC's Q4 attribution point). 0.6 = arya ≥60% of the turn.
    voice_latency_attribution: float = 0.6
    # Don't re-offer Turbo more than once per this window per room (no nagging after a "no"/ignore).
    voice_latency_offer_cooldown_secs: float = 600.0
    # How long the held command-window / pending offer stays valid for the user's "yes" before it expires.
    voice_latency_offer_ttl_secs: float = 60.0
    # Recovery probing (only while in Turbo AND no room is producing a real arya sample): cadence + a hard
    # per-day cap so a flapping arya can't rack up probe cost.
    voice_latency_probe_secs: float = 75.0
    voice_latency_probe_daily_max: int = 40
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
    # Media transport-intent signal (movie-night wake-media-pause resume-suppression). When a turn issues a
    # user PAUSE/STOP of playing media, the turn emits a `transport_intent` SSE event so the voice side's
    # wake-media-pauser does NOT auto-resume the movie it paused on wake — the user paused it themselves, so
    # leave it paused ("Hey Aria, pause the movie"). False disables the emit only (the pause command still
    # runs + confirms); the voice side then degrades to resume-unless-session-closed. Kill-switch.
    # Env: GABAI_VOICE_TRANSPORT_INTENT.
    voice_transport_intent: bool = True
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
    # Route the is_addressed gate classify to the fast Claude classifier (haiku) on EVERY turn, not just in
    # Turbo. The gate fires on every voice turn; on arya it rides Gab cloud-latency variance and spiked to
    # ~10s on the Pi (2026-06-23 live drive) — the dominant felt-latency tail once B(ii) skips intent_classify
    # on most turns. Haiku bounds the gate to ~0.4-0.6s and is spike-proof (off arya's variance), with the
    # same suppression behavior; conversation answers stay on arya. Costs one haiku classification call per
    # turn (a billing tradeoff — hence opt-in). No-ops to arya when no Claude backend is configured/cross-
    # enabled. Default False = historical behavior (arya gate, free). Env: GABAI_VOICE_FAST_ADDRESSING_GATE.
    # NOTE: as of 2026-06-26 the gate ALSO prefers haiku by DEFAULT whenever a Claude backend is available
    # cross-backend (see voice_arya_addressing_gate) — so this flag is now only needed to FORCE haiku when
    # you've opted back into the arya gate. It still works as before (forces haiku) for back-compat.
    voice_fast_addressing_gate: bool = False
    # Escape hatch: force the addressing-gate classify back onto arya (the historical free gate) even when a
    # Claude backend is available. Default False = prefer the cheaper, spike-proof haiku classifier whenever
    # Claude is configured + cross-enabled and the primary provider isn't already Claude (2026-06-26: a fresh
    # loopback satellite that set neither Turbo nor voice_fast_addressing_gate was silently paying arya's
    # ~4.8s gate-tax — the invisible-config trap). Turbo / voice_fast_addressing_gate still override this
    # (explicit opt-in to haiku wins). No-ops when no Claude backend is available (already arya). Env:
    # GABAI_VOICE_ARYA_ADDRESSING_GATE.
    voice_arya_addressing_gate: bool = False
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
    # Liveness gone-timeout for the deferred-announce channel (voice/announce_store.py, the GET /builder/poll
    # proactive channel). An announcement claimed by a polling device is held while that device keeps polling;
    # it reverts to free-for-all only after this many seconds with NO poll from the claimer (≈ a few missed
    # poll cycles ⇒ the device is gone/shut down). Also the originating-first fallback grace. NOT a speak-
    # deadline: the voice floor can stay closed indefinitely while asleep, so delivery must key on poller
    # liveness, never on elapsed time-since-ready. Default comfortably exceeds the ~1.5s voice poll cadence.
    # Env: GABAI_VOICE_ANNOUNCE_LEASE_SECS.
    voice_announce_lease_secs: float = 8.0
    # This machine's friendly name, used to tag media sources as LOCAL vs on another device/room (e.g.
    # "HomeServer"). Empty → defaults to the hostname at use. The brain only AUTO-ducks/controls media it
    # judges local to this device; remote sources are visible (for future explicit control) but never touched
    # automatically. Env: GABAI_LOCAL_DEVICE.
    local_device: str = ""
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    attestation: AttestationConfig = Field(default_factory=AttestationConfig)
    jellyfin: JellyfinConfig = Field(default_factory=JellyfinConfig)
    radarr: RadarrConfig = Field(default_factory=RadarrConfig)
    sonarr: SonarrConfig = Field(default_factory=SonarrConfig)
    tidal: TidalConfig = Field(default_factory=TidalConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    image: ImageGenConfig = Field(default_factory=ImageGenConfig)
    tmdb: TmdbConfig = Field(default_factory=TmdbConfig)
    moviescout: MoviescoutConfig = Field(default_factory=MoviescoutConfig)
    tmi: TmiConfig = Field(default_factory=TmiConfig)
    # Phase 10 / #62: per-room media-target overrides, keyed by room_id. Empty default = no override on any
    # room = today's single-target behavior (byte-identical). Written by the future `gab detect-media` sync.
    room_media: dict[str, RoomMediaProfile] = Field(default_factory=dict)
