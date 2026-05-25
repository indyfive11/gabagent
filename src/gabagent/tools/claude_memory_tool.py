from __future__ import annotations
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry
from gabagent.api.models import ToolResult

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


def _memory_dir(scope: str, cwd: Path | None) -> Path:
    home = Path.home()
    base = home / ".claude" / "projects"
    if scope == "global":
        key = "-" + str(home).lstrip("/").replace("/", "-")
    else:
        target = (cwd or Path.cwd()).resolve()
        key = "-" + str(target).lstrip("/").replace("/", "-")
    return base / key / "memory"


@registry.register
class ReadClaudeMemoryTool(ToolBase):
    name = "read_claude_memory"
    description = (
        "Read Claude Code's persistent memory files. "
        "Use scope='project' for memories specific to the current project, "
        "or scope='global' for user-level memories shared across all projects. "
        "Only call this when the user explicitly asks you to read Claude's memory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["project", "global"],
                "description": "'project' = current project memories, 'global' = user-level memories",
            },
        },
        "required": ["scope"],
    }

    async def execute(self, ctx: AgentContext, scope: str = "project", **kwargs: Any) -> ToolResult:
        mem_dir = _memory_dir(scope, ctx.cwd)
        index = mem_dir / "MEMORY.md"

        if not mem_dir.exists():
            return ToolResult(
                output="",
                error=f"No Claude memory found at {mem_dir}",
            )

        parts: list[str] = []

        # Always include the index
        if index.exists():
            parts.append(f"## MEMORY.md (index)\n\n{index.read_text(encoding='utf-8').strip()}")

        # Read each individual memory file referenced alongside the index
        for md in sorted(mem_dir.glob("*.md")):
            if md.name == "MEMORY.md":
                continue
            try:
                parts.append(f"## {md.name}\n\n{md.read_text(encoding='utf-8').strip()}")
            except Exception:
                pass

        if not parts:
            return ToolResult(output="(memory directory exists but contains no files)")

        label = "project" if scope == "project" else "global user"
        header = f"Claude Code {label} memory ({mem_dir}):\n\n"
        return ToolResult(output=header + "\n\n---\n\n".join(parts))
