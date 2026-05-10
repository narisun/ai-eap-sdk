"""MCP stdio server — wraps McpToolRegistry as an MCP server."""

from __future__ import annotations

from typing import Any

from eap_core.mcp.registry import McpToolRegistry


def build_mcp_server(registry: McpToolRegistry, *, server_name: str = "eap-core") -> Any:
    """Build an MCP server that exposes all tools in `registry` over stdio."""
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError as e:
        raise ImportError(
            "MCP stdio server requires the [mcp] extra: pip install eap-core[mcp]"
        ) from e

    server: Any = Server(server_name)

    @server.list_tools()  # type: ignore[untyped-decorator,unused-ignore]
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for spec in registry.list_tools()
        ]

    @server.call_tool()  # type: ignore[untyped-decorator,unused-ignore]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = await registry.invoke(name, arguments)
        return [TextContent(type="text", text=str(result))]

    return server


async def run_stdio(registry: McpToolRegistry, *, server_name: str = "eap-core") -> None:
    """Convenience entry point: build the server and run it over stdio."""
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as e:
        raise ImportError("MCP stdio runner requires the [mcp] extra") from e
    server = build_mcp_server(registry, server_name=server_name)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
