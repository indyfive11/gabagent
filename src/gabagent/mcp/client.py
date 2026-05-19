from __future__ import annotations
import asyncio
import json
import subprocess
from typing import Any


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args
        self.env = env or {}
        self._proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._capabilities: dict = {}

    async def start(self) -> None:
        import os
        env = {**os.environ, **self.env}
        self._proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        if self._proc is None:
            raise RuntimeError("MCP client not started")
        self._req_id += 1
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }) + "\n"
        self._proc.stdin.write(req.encode())
        await self._proc.stdin.drain()
        line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30)
        return json.loads(line.decode()).get("result")

    async def initialize(self) -> dict:
        result = await self._rpc("initialize", {
            "protocolVersion": "0.1.0",
            "capabilities": {},
            "clientInfo": {"name": "gabagent", "version": "0.1.0"},
        })
        self._capabilities = result or {}
        return self._capabilities

    async def list_tools(self) -> list[dict]:
        result = await self._rpc("tools/list")
        return (result or {}).get("tools", [])

    async def call_tool(self, name: str, args: dict) -> str:
        result = await self._rpc("tools/call", {"name": name, "arguments": args})
        if result:
            content = result.get("content", [])
            return "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        return ""

    async def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            await self._proc.wait()
