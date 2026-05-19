from __future__ import annotations
from typing import Any
from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry
from gabagent.session.postmortem import PostMortemManager

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class PostMortemLogTool(ToolBase):
    name = "postmortem_log"
    description = "Log a post-mortem investigation for a crash or error."
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title of the incident"},
            "description": {"type": "string", "description": "Detailed description of the incident and resolution"},
        },
        "required": ["title", "description"],
    }

    async def execute(self, ctx: AgentContext, title: str, description: str, **kwargs: Any) -> ToolResult:
        mgr = PostMortemManager(ctx.cwd)
        mgr.log(title, description)
        return ToolResult(output=f"Post-mortem logged: {title}")
