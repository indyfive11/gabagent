from __future__ import annotations
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class ReadFileTool(ToolBase):
    name = "read_file"
    description = (
        "Read the contents of a file. Returns text with line numbers. "
        "Supports text files. For images returns a description of the path."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "offset": {"type": "integer", "description": "Line number to start from (1-indexed)"},
            "limit": {"type": "integer", "description": "Maximum number of lines to read"},
        },
        "required": ["path"],
    }

    async def execute(
        self,
        ctx: AgentContext,
        path: str,
        offset: int = 1,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ToolResult:
        p = _resolve(path, ctx.cwd)
        if not p.exists():
            return ToolResult(output="", error=f"File not found: {path}")
        if p.is_dir():
            return ToolResult(output="", error=f"Path is a directory: {path}")

        suffix = p.suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"):
            return ToolResult(output=f"[image file: {p}]")

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(output="", error=str(e))

        lines = text.splitlines(keepends=True)
        start = max(0, (offset or 1) - 1)
        end = start + limit if limit else len(lines)
        selected = lines[start:end]

        numbered = "".join(
            f"{start + i + 1}\t{line}" for i, line in enumerate(selected)
        )
        return ToolResult(output=numbered or "(empty file)")


@registry.register
class WriteFileTool(ToolBase):
    name = "write_file"
    description = "Create or overwrite a file with the given content."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }

    async def execute(
        self, ctx: AgentContext, path: str, content: str, **kwargs: Any
    ) -> ToolResult:
        if ctx.plan_mode:
            return ToolResult(output="", error="Blocked in plan mode: write_file")
        p = _resolve(path, ctx.cwd)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return ToolResult(output="", error=str(e))
        lines = content.count("\n") + 1
        return ToolResult(output=f"Written {lines} lines to {p}")


@registry.register
class EditTool(ToolBase):
    name = "edit"
    description = (
        "Perform an exact string replacement in a file. "
        "old_string must appear EXACTLY ONCE in the file. "
        "You MUST have read the file before editing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to edit"},
            "old_string": {"type": "string", "description": "Exact string to replace"},
            "new_string": {"type": "string", "description": "Replacement string"},
        },
        "required": ["path", "old_string", "new_string"],
    }

    async def execute(
        self,
        ctx: AgentContext,
        path: str,
        old_string: str,
        new_string: str,
        **kwargs: Any,
    ) -> ToolResult:
        if ctx.plan_mode:
            return ToolResult(output="", error="Blocked in plan mode: edit")
        p = _resolve(path, ctx.cwd)
        if not p.exists():
            return ToolResult(output="", error=f"File not found: {path}")
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return ToolResult(output="", error=str(e))

        count = content.count(old_string)
        if count == 0:
            return ToolResult(output="", error="String not found in file")
        if count > 1:
            return ToolResult(
                output="",
                error=f"String appears {count} times — provide more context to make it unique",
            )

        new_content = content.replace(old_string, new_string, 1)
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            tmp.write_text(new_content, encoding="utf-8")
            os.replace(tmp, p)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return ToolResult(output="", error=str(e))
        return ToolResult(output=f"Edited {p}: replaced 1 occurrence")


@registry.register
class GlobTool(ToolBase):
    name = "glob"
    description = "Find files matching a glob pattern."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. src/**/*.py)"},
            "cwd": {"type": "string", "description": "Directory to search from (default: project root)"},
        },
        "required": ["pattern"],
    }

    async def execute(
        self, ctx: AgentContext, pattern: str, cwd: str | None = None, **kwargs: Any
    ) -> ToolResult:
        import subprocess
        base = Path(cwd) if cwd else ctx.cwd
        try:
            result = subprocess.run(
                ["fd", "--glob", pattern, "--type", "f", ".", str(base)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                return ToolResult(output="\n".join(lines[:200]) or "(no matches)")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback to pathlib
        try:
            matches = sorted(base.rglob(pattern))[:200]
            return ToolResult(output="\n".join(str(m) for m in matches) or "(no matches)")
        except Exception as e:
            return ToolResult(output="", error=str(e))


def _resolve(path: str, cwd: Path) -> Path:
    # Brain owns spoken→token assembly for file commands: a path dictated as "slash tmp slash notes dot
    # txt" becomes /tmp/notes.txt. Guarded on the WORD "slash", so an already-clean path is untouched.
    from gabagent.voice.spoken_tokens import maybe_assemble_path
    p = Path(maybe_assemble_path(path)).expanduser()
    if p.is_absolute():
        return p
    return cwd / p
