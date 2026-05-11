"""mcp-server-example — standalone MCP-stdio server.

Run as a subprocess from any MCP-aware client (Claude Desktop,
Claude Code, IDE extensions, or another EAP-Core agent):

    python server.py

Tools are auto-registered when their modules import — see ``tools/``.
Add new tools with: ``eap create-tool --name <name> --mcp``.
"""

from __future__ import annotations

import asyncio

from tools.example_tool import echo

from eap_core.mcp import McpToolRegistry
from eap_core.mcp.server import run_stdio

REGISTRY = McpToolRegistry()
REGISTRY.register(echo.spec)


async def main() -> None:
    await run_stdio(REGISTRY, server_name="mcp-server-example")


if __name__ == "__main__":
    asyncio.run(main())
