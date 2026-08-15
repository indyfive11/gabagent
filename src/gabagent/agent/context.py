from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gabagent.api.client import GabAIClient
    from gabagent.api.rate_limit import RateLimiter
    from gabagent.config.models import GabAgentConfig
    from gabagent.session.serializer import SessionFile
    from gabagent.tools.shell_tool import ShellState


@dataclass
class AgentContext:
    config: GabAgentConfig
    client: GabAIClient
    rate_limiter: RateLimiter
    session: SessionFile
    session_id: str
    cwd: Path = field(default_factory=Path.cwd)
    # Durable per-room key for the Tiered-Memory layer (process-per-room topology): which physical
    # room/satellite this brain serves, e.g. "living_room". Set once at launch (--room-id) and refreshed
    # from the VoiceSession on each turn. None ⇒ single-room/TUI ⇒ memory uses a shared 'default' bucket
    # (exact current behavior). Inert until the TMI layer (config.tmi.enabled) reads it.
    room_id: str | None = None
    system_prompt: str = ""
    plan_mode: bool = False
    plan_file_path: Path | None = None
    headless: bool = False
    shell_state: ShellState | None = None
    token_estimate: int = 0
    active_model: str | None = None
    active_effort: str | None = None  # thinking effort for the current turn (Claude provider)
    active_backend: str | None = None  # backend serving the current turn: "claude" | "gab" | "local"
    force_model: bool = False
    # Runtime model PIN (session-only): when force_model is True and pinned_backend is set, every turn
    # runs on exactly this (model, effort, backend) with NO escalation, until unpinned (/model auto).
    pinned_model: str | None = None
    pinned_effort: str | None = None
    pinned_backend: str | None = None
    # Turbo Mode (session-only, voice-toggled): when True, COMMAND-intent turns route to the fast rung
    # (Claude haiku) instead of arya and skip the arya classify, while CONVERSATION turns stay on the
    # normal rung. Distinct from force_model (a hard per-turn pin of ALL turns). "regular mode" clears it.
    turbo_commands: bool = False
    local_client: Any = field(default=None)
    local_mode: bool = False
    local_process: Any = field(default=None)
    local_context_summary: str | None = None
    local_floor: bool = False  # local is the persisted warm bottom rung (mirrors config.local_floor)
    # Set when an internet outage AUTO-failed this session over to the local model (distinct from a
    # manual "switch to local", which leaves this False). Gates the per-turn cloud-recovery probe so
    # only an auto-failover reverts to the cloud when the connection returns; a deliberate local pick stays.
    offline_failover: bool = False
    # Anti-flap for the auto offline-failover. `cloud_probe_after` rate-limits the per-turn cloud
    # recovery probe (a REAL completion, not a bare GET) to once per interval while failed over, so a
    # host that answers a GET while its streaming endpoint is still down can't trigger a premature
    # revert. `offline_notice_at` timestamps the last spoken outage notice so a fast up/down flap can't
    # re-announce every cycle. Both monotonic seconds; 0.0 = never/re-armed.
    cloud_probe_after: float = 0.0
    offline_notice_at: float = 0.0
    # Lazily-built per-backend clients keyed "gab" | "claude" | "local", for the cross-backend
    # escalation ladder. `client`/`local_client` remain for back-compat and the exclusive local_mode.
    clients: dict[str, Any] = field(default_factory=dict)
    # Backends that HARD-failed this session (402 billing / auth / missing model). The assembled
    # ladder skips them so the floor moves up; cleared on restart. Announced once when first marked.
    degraded_backends: set = field(default_factory=set)
    # Voice mode (all inert for TUI sessions).
    voice_mode: bool = False
    approval_hook: Any = field(default=None)  # async (tool_name, args, ctx) -> bool
    voice_emit: Any = field(default=None)     # async (VoiceEvent) -> None, set per-turn
    voice_session: Any = field(default=None)  # VoiceSession (confirm futures, undo, arming)
    voice_audit_path: Any = field(default=None)  # Path | None
    voice_debug_path: Any = field(default=None)  # Path | None — opt-in per-turn debug log
    voice_process: Any = field(default=None)  # subprocess.Popen | None — TUI-spawned voice brain
    voice_frontend_process: Any = field(default=None)  # subprocess.Popen | None — TUI-spawned voice-agent front-end
    command_catalog: Any = field(default=None)  # CommandCatalog | None — discovered capabilities
    persistent_browser: Any = field(default=None)  # Playwright BrowserContext | None
    persistent_browser_pw: Any = field(default=None)  # Playwright instance handle | None
    jellyfin_playing_page: Any = field(default=None)  # Playwright Page driving a browser-launched movie
    jellyfin_paused: bool = field(default=False)      # our tracked play/pause state for that page
    voice_announced_model: Any = field(default=None)  # last model we spoke an "escalating" notice for
    # Stratum (memory subsystem) — all inert unless config.stratum.enabled. is_subagent gates Stratum
    # OFF inside sub-agents (they must not rewrite the parent's Current Focus or accrete habits).
    is_subagent: bool = False
    stratum_signals: list = field(default_factory=list)   # in-memory per-turn observer signals (never disk)
    stratum_seen_calls: dict = field(default_factory=dict)  # (tool,args) → count, for repeat detection
