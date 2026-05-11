"""MCP stdio server — wraps McpToolRegistry as an MCP server."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from eap_core.mcp.registry import McpToolRegistry


def _serialize_for_text_content(result: Any) -> str:
    """Serialize a tool result for embedding in MCP TextContent.text.

    Routes pydantic ``BaseModel`` through ``model_dump_json`` and
    ``dict``/``list`` through ``json.dumps`` so external MCP clients
    receive parseable JSON. Primitives (str/int/bool/None) preserve
    the original ``str()`` behavior — backward-compatible for tools
    that return raw text.

    Prior to v0.7.1 every result went through ``str()``; for
    ``BaseModel`` returns that emitted a Python-specific repr
    (``name='x' count=1``) instead of JSON, which non-Python MCP
    clients couldn't parse.
    """
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=str)
    return str(result)


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
        return [TextContent(type="text", text=_serialize_for_text_content(result))]

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
