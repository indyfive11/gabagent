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
