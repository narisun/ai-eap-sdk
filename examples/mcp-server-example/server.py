"""mcp-server-example — standalone MCP-stdio server.

Run as a subprocess from any MCP-aware client (Claude Desktop,
Claude Code, IDE extensions, or another EAP-Core agent):

    python server.py

Tools are auto-registered when their modules import — see ``tools/``.
Add new tools with: ``eap create-tool --name <name> --mcp``.
"""

from __future__ import annotations

import asyncio

# Importing the tool modules triggers @mcp_tool registration on import.
from tools import example_tool  # noqa: F401

from eap_core.mcp import default_registry
from eap_core.mcp.server import run_stdio


async def main() -> None:
    await run_stdio(default_registry(), server_name="mcp-server-example")


if __name__ == "__main__":
    asyncio.run(main())
