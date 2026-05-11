"""Example MCP tool — register one and ship.

Add more tools with ``eap create-tool --name <name> --mcp`` from this
project's root. The decorator generates JSON Schema from your type
hints; the server module wires the tool into its own ``McpToolRegistry``
which the stdio loop exposes.
"""

from __future__ import annotations

from eap_core.mcp import mcp_tool


@mcp_tool(description="Echo the input back. Replace with real tool logic.")
async def echo(message: str) -> str:
    return message
