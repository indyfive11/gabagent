from __future__ import annotations
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_READONLY_PREFIXES = (
    "ls", "cat", "grep", "rg", "fd", "find", "git log", "git diff",
    "git status", "git show", "git blame", "echo", "head", "tail",
    "wc", "file", "which", "type", "pwd", "env", "printenv",
)

_MUTATING_SIGNALS = (">", ">>", "rm ", "mv ", "cp ", "mkdir", "touch",
                     "install", "pip ", "npm ", "yarn ", "apt ", "pacman ",
                     "sudo ", "chmod", "chown", "tee ", "write")


def is_bash_readonly(command: str) -> bool:
    cmd = command.strip()
    for sig in _MUTATING_SIGNALS:
        if sig in cmd:
            return False
    for prefix in _READONLY_PREFIXES:
        if cmd.startswith(prefix):
            return True
    return False


def enter_plan_mode(ctx: AgentContext) -> None:
    ctx.plan_mode = True
    ctx.plan_file_path = ctx.cwd / ".claude" / f"plan-{uuid.uuid4().hex[:8]}.md"
    ctx.plan_file_path.parent.mkdir(parents=True, exist_ok=True)
    plan_notice = (
        "\n\n---\n# PLAN MODE ACTIVE\n"
        "You are in read-only exploration mode. "
        "Writes, edits, and shell mutations are BLOCKED. "
        f"You may write your plan to: {ctx.plan_file_path}\n"
        "Use the write_plan tool to save your plan."
    )
    ctx.system_prompt = ctx.system_prompt + plan_notice


def exit_plan_mode(ctx: AgentContext) -> None:
    ctx.plan_mode = False
    ctx.system_prompt = ctx.system_prompt.split("\n\n---\n# PLAN MODE ACTIVE")[0]


@registry.register
class WritePlanTool(ToolBase):
    name = "write_plan"
    description = "Write or update the plan document (allowed even in plan mode)."
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The plan content (markdown)"},
        },
        "required": ["content"],
    }

    async def execute(self, ctx: AgentContext, content: str, **kwargs: Any) -> ToolResult:
        if not ctx.plan_file_path:
            ctx.plan_file_path = ctx.cwd / ".claude" / f"plan-{uuid.uuid4().hex[:8]}.md"
            ctx.plan_file_path.parent.mkdir(parents=True, exist_ok=True)
        ctx.plan_file_path.write_text(content, encoding="utf-8")

        if not ctx.plan_mode:
            ctx.plan_mode = True
            ctx.system_prompt += (
                "\n\n---\n# PLAN MODE ACTIVE\n"
                "You are now in plan mode. Writes, edits, and shell mutations are BLOCKED. "
                f"Plan file: {ctx.plan_file_path}\n"
                "Present a summary of the plan to the user and STOP. "
                "Do NOT write files, run commands, or make edits. "
                "Wait for the user to type /approve before proceeding."
            )

        return ToolResult(
            output=(
                f"Plan written to {ctx.plan_file_path}. "
                "PLAN MODE ACTIVE — present the plan summary to the user and STOP. "
                "Do not execute anything. Wait for /approve."
            )
        )
