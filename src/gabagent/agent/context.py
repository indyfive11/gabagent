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
    system_prompt: str = ""
    plan_mode: bool = False
    plan_file_path: Path | None = None
    headless: bool = False
    shell_state: ShellState | None = None
    token_estimate: int = 0
    active_model: str | None = None
    force_model: bool = False
    local_client: Any = field(default=None)
    local_mode: bool = False
    local_process: Any = field(default=None)
    local_context_summary: str | None = None
    # Voice mode (all inert for TUI sessions).
    voice_mode: bool = False
    approval_hook: Any = field(default=None)  # async (tool_name, args, ctx) -> bool
    voice_emit: Any = field(default=None)     # async (VoiceEvent) -> None, set per-turn
    voice_session: Any = field(default=None)  # VoiceSession (confirm futures, undo, arming)
    voice_audit_path: Any = field(default=None)  # Path | None
    voice_debug_path: Any = field(default=None)  # Path | None — opt-in per-turn debug log
    voice_process: Any = field(default=None)  # subprocess.Popen | None — TUI-spawned voice brain
    command_catalog: Any = field(default=None)  # CommandCatalog | None — discovered capabilities
    persistent_browser: Any = field(default=None)  # Playwright BrowserContext | None
    persistent_browser_pw: Any = field(default=None)  # Playwright instance handle | None
    jellyfin_playing_page: Any = field(default=None)  # Playwright Page driving a browser-launched movie
    jellyfin_paused: bool = field(default=False)      # our tracked play/pause state for that page
