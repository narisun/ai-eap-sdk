"""`eap create-mcp-server` scaffolder."""

from __future__ import annotations

from pathlib import Path

from eap_cli.scaffolders.render import render_template_dir


def _templates_root() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def create_mcp_server(
    target: Path,
    *,
    server_name: str,
    force: bool = False,
) -> list[Path]:
    """Scaffold a standalone MCP-stdio server project.

    The generated project has no LLM client; it just registers EAP-Core
    `@mcp_tool`-decorated functions with the registry and runs them as an
    MCP stdio server. Other agents (or the user's IDE) connect via the
    MCP protocol.
    """
    src = _templates_root() / "mcp_server"
    return render_template_dir(
        src,
        target,
        {"server_name": server_name},
        force=force,
    )
