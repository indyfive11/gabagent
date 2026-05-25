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
        "Read Claude Code's persistent memory. "
        "Use scope='project' for current project memories, scope='global' for user-level memories. "
        "By default returns only the MEMORY.md index (concise). "
        "Set full=true to read all individual memory files (large — use sparingly). "
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
            "full": {
                "type": "boolean",
                "description": "If true, read all individual memory files in addition to the index. Large output.",
            },
            "file": {
                "type": "string",
                "description": "Read a specific memory file by name (e.g. 'user_role.md'). Overrides full.",
            },
        },
        "required": ["scope"],
    }

    async def execute(
        self,
        ctx: AgentContext,
        scope: str = "project",
        full: bool = False,
        file: str | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        mem_dir = _memory_dir(scope, ctx.cwd)
        index = mem_dir / "MEMORY.md"
        label = "project" if scope == "project" else "global user"

        if not mem_dir.exists():
            return ToolResult(output="", error=f"No Claude memory found at {mem_dir}")

        # Specific file requested
        if file:
            target = mem_dir / file
            if not target.exists():
                return ToolResult(output="", error=f"{file} not found in {mem_dir}")
            return ToolResult(output=f"## {file}\n\n{target.read_text(encoding='utf-8').strip()}")

        parts: list[str] = []

        if index.exists():
            parts.append(f"## MEMORY.md (index)\n\n{index.read_text(encoding='utf-8').strip()}")

        if not parts:
            return ToolResult(output="(memory directory exists but contains no files)")

        if full:
            for md in sorted(mem_dir.glob("*.md")):
                if md.name == "MEMORY.md":
                    continue
                try:
                    parts.append(f"## {md.name}\n\n{md.read_text(encoding='utf-8').strip()}")
                except Exception:
                    pass

        header = f"Claude Code {label} memory ({mem_dir}):\n\n"
        return ToolResult(output=header + "\n\n---\n\n".join(parts))
