from __future__ import annotations
from pathlib import Path
from gabagent.config.paths import memory_file
from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


class MemoryManager:
    def __init__(self, cwd: Path | None = None):
        self._path = memory_file(cwd)

    def load(self) -> str:
        if self._path.exists():
            try:
                return self._path.read_text(encoding="utf-8")
            except Exception:
                pass
        return ""

    def append(self, content: str) -> None:
        import time
        ts = time.strftime("%Y-%m-%d %H:%M")
        entry = f"\n## {ts}\n\n{content.strip()}\n"
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(entry)

    def rewrite(self, content: str) -> None:
        self._path.write_text(content, encoding="utf-8")

    def clear(self) -> None:
        self._path.write_text("")

    def pop_last(self) -> bool:
        """Remove the most recent '## <timestamp>' note block. Returns False if none."""
        if not self._path.exists():
            return False
        lines = self._path.read_text(encoding="utf-8").splitlines()
        idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("## ")), None)
        if idx is None:
            return False
        self._path.write_text("\n".join(lines[:idx]).rstrip() + "\n", encoding="utf-8")
        return True

    def health_check(self) -> None:
        """Archiving logic: if memory file > 200 lines, archive oldest and keep newest."""
        if not self._path.exists():
            return
            
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 200:
            return
            
        # Split: keep last 50 lines in memory, archive the rest
        keep, archive = lines[-50:], lines[:-50]
        
        archive_path = self._path.with_name(f"{self._path.stem}-archive.md")
        with archive_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(archive) + "\n")
            
        self._path.write_text("\n".join(keep), encoding="utf-8")


@registry.register
class MemoryWriteTool(ToolBase):
    name = "memory_write"
    description = (
        "Save a note to persistent memory that will be available in future sessions. "
        "Use this to remember important facts, decisions, or context about this project."
    )
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The note to save to memory"},
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace all memory with this content instead of appending",
            },
        },
        "required": ["content"],
    }

    async def execute(self, ctx: AgentContext, content: str, replace_all: bool = False, **kwargs: Any) -> ToolResult:
        mgr = MemoryManager(ctx.cwd)
        if replace_all:
            mgr.rewrite(content)
        else:
            mgr.append(content)
            mgr.health_check()
            
        ctx.system_prompt = _rebuild_system_prompt(ctx)
        return ToolResult(output=f"Memory {'replaced' if replace_all else 'updated'}.")


@registry.register
class MemoryReadTool(ToolBase):
    name = "memory_read"
    description = "Read the current persistent memory for this project."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
        mgr = MemoryManager(ctx.cwd)
        content = mgr.load()
        if not content:
            return ToolResult(output="(no memory saved yet)")
        return ToolResult(output=content)


def _rebuild_system_prompt(ctx: AgentContext) -> str:
    from gabagent.agent.system_prompt import build_system_prompt
    from gabagent.session.memory import MemoryManager
    mgr = MemoryManager(ctx.cwd)
    memory = mgr.load()
    return build_system_prompt(cwd=ctx.cwd, memory=memory or None)
