"""Top-level Click app for `eap`."""
from __future__ import annotations

from pathlib import Path

import click

from eap_cli.scaffolders.create_agent import create_agent
from eap_cli.scaffolders.create_tool import create_tool
from eap_cli.scaffolders.init import init_project


@click.group()
@click.version_option()
def cli() -> None:
    """EAP-Core CLI — scaffold and operate agentic AI projects."""


@cli.command("init")
@click.argument("target", type=click.Path(file_okay=False, path_type=Path))
@click.option("--name", default=None, help="Project name (defaults to target dir name).")
@click.option("--runtime", type=click.Choice(["local", "bedrock", "vertex"]), default="local")
@click.option("--force", is_flag=True, help="Overwrite existing files.")
def init_cmd(target: Path, name: str | None, runtime: str, force: bool) -> None:
    """Scaffold a new EAP-Core agent project."""
    project_name = name or target.name
    try:
        written = init_project(target, project_name=project_name, runtime=runtime, force=force)
    except FileExistsError as e:
        raise click.ClickException(f"{e}. Re-run with --force to overwrite.") from e
    click.echo(f"Wrote {len(written)} files to {target}")


@cli.command("create-agent")
@click.option("--name", required=True, help="Agent name (used in template variables).")
@click.option(
    "--template",
    required=True,
    type=click.Choice(["research", "transactional"]),
)
def create_agent_cmd(name: str, template: str) -> None:
    """Generate an agent from a template (overlays the current project)."""
    target = Path.cwd()
    written = create_agent(target, agent_name=name, template=template)
    click.echo(f"Wrote {len(written)} files for {template} template.")


@cli.command("create-tool")
@click.option("--name", required=True, help="Tool name (becomes the function and filename).")
@click.option("--mcp", "as_mcp", is_flag=True, help="Generate as MCP-decorated tool. Required.")
@click.option("--auth-required", is_flag=True, help="Mark the tool as requires_auth=True.")
def create_tool_cmd(name: str, as_mcp: bool, auth_required: bool) -> None:
    """Generate a new MCP tool stub."""
    if not as_mcp:
        raise click.ClickException("Only --mcp tools are supported in this version. Pass --mcp.")
    written = create_tool(Path.cwd(), name=name, requires_auth=auth_required)
    click.echo(f"Created {len(written)} file(s) for tool {name!r}.")


if __name__ == "__main__":
    cli()
