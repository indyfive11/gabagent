from __future__ import annotations
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class SendToClaudeTool(ToolBase):
    name = "send_to_claude"
    description = (
        "Send a message to Claude Code (Rob's other AI assistant). "
        "Use this to share findings, ask questions, or flag issues for Claude Code to act on. "
        "Messages are queued and Claude Code reads them from ~/.config/gabagent/bridge/from_gab.jsonl."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to send"},
            "topic": {
                "type": "string",
                "description": "Short topic label (e.g. 'bug', 'question', 'note')",
                "default": "note",
            },
        },
        "required": ["message"],
    }

    async def execute(
        self, ctx: AgentContext, message: str, topic: str = "note", **kwargs: Any
    ) -> ToolResult:
        try:
            from gabagent.bridge import gab_to_claude
            gab_to_claude(message, topic=topic)
            return ToolResult(output=f"Message sent to Claude Code (topic: {topic}).")
        except Exception as e:
            return ToolResult(output="", error=str(e))


@registry.register
class CheckInboxTool(ToolBase):
    name = "check_inbox"
    description = (
        "Check for new messages from Claude Code. "
        "Returns any unread messages and marks them as read."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        try:
            from gabagent.bridge import read_inbox
            messages = read_inbox(mark_read=True)
            if not messages:
                return ToolResult(output="No new messages from Claude Code.")
            lines = []
            for msg in messages:
                topic = msg.get("topic", "note")
                lines.append(f"[{topic}] {msg['message']}")
            return ToolResult(output="\n\n".join(lines))
        except Exception as e:
            return ToolResult(output="", error=str(e))
