from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


class LSPClient:
    def __init__(self, language: str, command: str):
        self.language = language
        self.command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._root_uri: str = ""

    async def start(self, workspace: Path) -> None:
        self._root_uri = workspace.as_uri()
        self._proc = await asyncio.create_subprocess_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._rpc("initialize", {
            "processId": os.getpid(),
            "rootUri": self._root_uri,
            "capabilities": {
                "textDocument": {
                    "definition": {"dynamicRegistration": False},
                    "references": {"dynamicRegistration": False},
                    "hover": {"dynamicRegistration": False},
                }
            },
        })
        await self._notify("initialized", {})

    async def _write(self, data: dict) -> None:
        body = json.dumps(data)
        header = f"Content-Length: {len(body)}\r\n\r\n"
        self._proc.stdin.write((header + body).encode())
        await self._proc.stdin.drain()

    async def _read(self) -> dict:
        header_line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=10)
        content_length = int(header_line.decode().split(":")[1].strip())
        await self._proc.stdout.readline()  # blank line
        body = await asyncio.wait_for(self._proc.stdout.read(content_length), timeout=10)
        return json.loads(body.decode())

    async def _rpc(self, method: str, params: dict) -> Any:
        self._req_id += 1
        await self._write({"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params})
        response = await self._read()
        return response.get("result")

    async def _notify(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def open_file(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        await self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": path.as_uri(),
                "languageId": self.language,
                "version": 1,
                "text": text,
            }
        })

    async def definition(self, path: Path, line: int, character: int) -> list[dict]:
        result = await self._rpc("textDocument/definition", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line - 1, "character": character},
        })
        if isinstance(result, dict):
            result = [result]
        return result or []

    async def references(self, path: Path, line: int, character: int) -> list[dict]:
        result = await self._rpc("textDocument/references", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line - 1, "character": character},
            "context": {"includeDeclaration": True},
        })
        return result or []

    async def hover(self, path: Path, line: int, character: int) -> str:
        result = await self._rpc("textDocument/hover", {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line - 1, "character": character},
        })
        if result and "contents" in result:
            contents = result["contents"]
            if isinstance(contents, dict):
                return contents.get("value", "")
            return str(contents)
        return ""

    async def stop(self) -> None:
        if self._proc:
            await self._rpc("shutdown", {})
            await self._notify("exit", {})
            await self._proc.wait()


_lsp_clients: dict[str, LSPClient] = {}


def _get_lsp_client(ctx: AgentContext, language: str) -> LSPClient | None:
    if language in _lsp_clients:
        return _lsp_clients[language]
    server_cmd = ctx.config.lsp.servers.get(language)
    if not server_cmd:
        return None
    client = LSPClient(language=language, command=server_cmd)
    _lsp_clients[language] = client
    return client


def _ext_to_lang(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript", ".go": "go",
        ".rs": "rust", ".c": "c", ".cpp": "cpp", ".java": "java",
    }.get(ext, "text")


@registry.register
class GoToDefinitionTool(ToolBase):
    name = "go_to_definition"
    description = "Find the definition of a symbol at a given file position using LSP."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "line": {"type": "integer", "description": "Line number (1-indexed)"},
            "character": {"type": "integer", "description": "Column/character offset"},
        },
        "required": ["path", "line", "character"],
    }

    async def execute(self, ctx: AgentContext, path: str, line: int, character: int, **kwargs: Any) -> ToolResult:
        lang = _ext_to_lang(path)
        client = _get_lsp_client(ctx, lang)
        if not client:
            return ToolResult(output="", error=f"No LSP server configured for {lang}")
        try:
            if not client._proc:
                await client.start(ctx.cwd)
            p = Path(path) if Path(path).is_absolute() else ctx.cwd / path
            await client.open_file(p)
            locations = await client.definition(p, line, character)
            if not locations:
                return ToolResult(output="(no definition found)")
            results = []
            for loc in locations:
                uri = loc.get("uri", "")
                r = loc.get("range", {}).get("start", {})
                file_path = uri.replace("file://", "")
                results.append(f"{file_path}:{r.get('line', 0) + 1}:{r.get('character', 0)}")
            return ToolResult(output="\n".join(results))
        except Exception as e:
            return ToolResult(output="", error=str(e))


@registry.register
class FindReferencesTool(ToolBase):
    name = "find_references"
    description = "Find all references to a symbol at a given file position using LSP."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "line": {"type": "integer", "description": "Line number (1-indexed)"},
            "character": {"type": "integer", "description": "Column/character offset"},
        },
        "required": ["path", "line", "character"],
    }

    async def execute(self, ctx: AgentContext, path: str, line: int, character: int, **kwargs: Any) -> ToolResult:
        lang = _ext_to_lang(path)
        client = _get_lsp_client(ctx, lang)
        if not client:
            return ToolResult(output="", error=f"No LSP server configured for {lang}")
        try:
            if not client._proc:
                await client.start(ctx.cwd)
            p = Path(path) if Path(path).is_absolute() else ctx.cwd / path
            await client.open_file(p)
            refs = await client.references(p, line, character)
            if not refs:
                return ToolResult(output="(no references found)")
            results = []
            for loc in refs:
                uri = loc.get("uri", "")
                r = loc.get("range", {}).get("start", {})
                file_path = uri.replace("file://", "")
                results.append(f"{file_path}:{r.get('line', 0) + 1}:{r.get('character', 0)}")
            return ToolResult(output="\n".join(results))
        except Exception as e:
            return ToolResult(output="", error=str(e))


@registry.register
class TypeInfoTool(ToolBase):
    name = "type_info"
    description = "Get type information (hover) for a symbol at a given file position using LSP."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "line": {"type": "integer", "description": "Line number (1-indexed)"},
            "character": {"type": "integer", "description": "Column/character offset"},
        },
        "required": ["path", "line", "character"],
    }

    async def execute(self, ctx: AgentContext, path: str, line: int, character: int, **kwargs: Any) -> ToolResult:
        lang = _ext_to_lang(path)
        client = _get_lsp_client(ctx, lang)
        if not client:
            return ToolResult(output="", error=f"No LSP server configured for {lang}")
        try:
            if not client._proc:
                await client.start(ctx.cwd)
            p = Path(path) if Path(path).is_absolute() else ctx.cwd / path
            await client.open_file(p)
            info = await client.hover(p, line, character)
            return ToolResult(output=info or "(no type info)")
        except Exception as e:
            return ToolResult(output="", error=str(e))
