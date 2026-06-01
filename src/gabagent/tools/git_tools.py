from __future__ import annotations
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


async def _git(ctx: AgentContext, args: str) -> tuple[str, int]:
    from gabagent.tools.shell_tool import BashTool
    tool = BashTool()
    result = await tool.execute(ctx, command=f"git {args}")
    return result.to_content(), result.exit_code


@registry.register
class GitDiffTool(ToolBase):
    name = "git_diff"
    description = "Show git diff of staged, unstaged, or specific commits."
    parameters = {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean", "description": "Show staged changes (default: false)"},
            "ref": {"type": "string", "description": "Commit ref or range (e.g. HEAD~1..HEAD)"},
        },
        "required": [],
    }

    async def execute(self, ctx: AgentContext, staged: bool = False, ref: str | None = None, **kwargs: Any) -> ToolResult:
        if ref:
            out, code = await _git(ctx, f"diff {ref}")
        elif staged:
            out, code = await _git(ctx, "diff --staged")
        else:
            out, code = await _git(ctx, "diff")
        return ToolResult(output=out or "(no changes)")


@registry.register
class GitLogTool(ToolBase):
    name = "git_log"
    description = "Show recent git commit history."
    parameters = {
        "type": "object",
        "properties": {
            "n": {"type": "integer", "description": "Number of commits (default: 10)"},
            "oneline": {"type": "boolean", "description": "Compact one-line format"},
        },
        "required": [],
    }

    async def execute(self, ctx: AgentContext, n: int = 10, oneline: bool = True, **kwargs: Any) -> ToolResult:
        fmt = "--oneline" if oneline else "--format='%h %an %s'"
        out, _ = await _git(ctx, f"log {fmt} -n {n}")
        return ToolResult(output=out or "(no commits)")


@registry.register
class GitStatusTool(ToolBase):
    name = "git_status"
    description = "Show the git working tree status."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        out, _ = await _git(ctx, "status")
        return ToolResult(output=out)


@registry.register
class GitCommitTool(ToolBase):
    name = "git_commit"
    description = (
        "Stage files and create a git commit. Shows diff for review before committing. "
        "Requires user approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Commit message"},
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to stage (empty = use already-staged files)",
            },
        },
        "required": ["message"],
    }
    allows_parallel = False

    async def execute(
        self,
        ctx: AgentContext,
        message: str,
        files: list[str] | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        if ctx.plan_mode:
            return ToolResult(output="", error="Blocked in plan mode: git_commit")

        if files:
            for f in files:
                await _git(ctx, f"add {f}")

        diff_out, _ = await _git(ctx, "diff --staged --stat")
        if not diff_out.strip():
            return ToolResult(output="", error="Nothing staged to commit")

        # In voice mode the permission gate (voice_approve, Tier 2 spoken yes/no)
        # already authorized this commit — skip the interactive stdin prompt,
        # which would otherwise hang the headless voice server.
        if not getattr(ctx, "voice_mode", False):
            from gabagent.tui.renderer import console, render_code
            console.print(render_code(diff_out, "diff"))

            from prompt_toolkit import PromptSession
            session = PromptSession()
            try:
                answer = await session.prompt_async(
                    f"Commit with message '{message}'? [y/N]: "
                )
            except (EOFError, KeyboardInterrupt):
                return ToolResult(output="", error="Commit cancelled")

            if answer.strip().lower() not in ("y", "yes"):
                return ToolResult(output="", error="Commit cancelled by user")

        msg_escaped = message.replace("'", "'\\''")
        out, code = await _git(ctx, f"commit -m '{msg_escaped}'")
        if code != 0:
            return ToolResult(output=out, error=f"Commit failed (exit {code})")
        return ToolResult(output=out)


@registry.register
class GitBranchTool(ToolBase):
    name = "git_branch"
    description = "List, create, or switch git branches."
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "switch"],
                "description": "Action to perform",
            },
            "name": {"type": "string", "description": "Branch name (for create/switch)"},
        },
        "required": ["action"],
    }

    async def execute(self, ctx: AgentContext, action: str, name: str | None = None, **kwargs: Any) -> ToolResult:
        if action == "list":
            out, _ = await _git(ctx, "branch -a")
            return ToolResult(output=out)
        elif action == "create":
            if not name:
                return ToolResult(output="", error="Branch name required")
            out, code = await _git(ctx, f"checkout -b {name}")
            return ToolResult(output=out, error=None if code == 0 else f"Exit {code}")
        elif action == "switch":
            if not name:
                return ToolResult(output="", error="Branch name required")
            out, code = await _git(ctx, f"checkout {name}")
            return ToolResult(output=out, error=None if code == 0 else f"Exit {code}")
        return ToolResult(output="", error=f"Unknown action: {action}")
