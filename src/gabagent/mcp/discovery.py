from __future__ import annotations
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext
    from .client import MCPClient


def _make_mcp_tool_class(client: MCPClient, tool_schema: dict) -> type[ToolBase]:
    tool_name = tool_schema["name"]
    tool_desc = tool_schema.get("description", "")
    tool_params = tool_schema.get("inputSchema", {"type": "object", "properties": {}, "required": []})

    class MCPTool(ToolBase):
        name = f"mcp_{client.name}_{tool_name}"
        description = f"[MCP:{client.name}] {tool_desc}"
        parameters = tool_params

        async def execute(self, ctx: AgentContext, **kwargs: Any) -> ToolResult:
            try:
                output = await client.call_tool(tool_name, kwargs)
                return ToolResult(output=output)
            except Exception as e:
                return ToolResult(output="", error=str(e))

    MCPTool.__name__ = f"MCPTool_{client.name}_{tool_name}"
    return MCPTool


async def connect_mcp_servers(ctx: AgentContext) -> list[str]:
    from gabagent.tui.renderer import console
    connected = []
    for server_name, server_cfg in ctx.config.mcp_servers.items():
        from .client import MCPClient
        client = MCPClient(
            name=server_name,
            command=server_cfg.command,
            args=server_cfg.args,
            env=server_cfg.env,
        )
        try:
            await client.start()
            await client.initialize()
            tools = await client.list_tools()
            for tool_schema in tools:
                cls = _make_mcp_tool_class(client, tool_schema)
                registry.register(cls)
            connected.append(server_name)
            console.print(f"[info]MCP: connected {server_name} ({len(tools)} tools)[/info]", markup=True)
        except Exception as e:
            console.print(f"[warning]MCP: failed to connect {server_name}: {e}[/warning]", markup=True)
    return connected
