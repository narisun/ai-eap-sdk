"""Top-level Click app for `eap`."""
from __future__ import annotations

from pathlib import Path

import click

from eap_cli.scaffolders.create_agent import create_agent
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


if __name__ == "__main__":
    cli()
