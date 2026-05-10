"""Top-level Click app for `eap`."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import click

from eap_cli.scaffolders.create_agent import Template, create_agent
from eap_cli.scaffolders.create_tool import create_tool
from eap_cli.scaffolders.deploy import (
    _real_deploy_enabled,
    deploy_gcp,
    package_aws,
    package_gcp,
    upload_aws,
)
from eap_cli.scaffolders.eval_cmd import run_eval
from eap_cli.scaffolders.init import Runtime, init_project


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
        written = init_project(
            target,
            project_name=project_name,
            runtime=cast(Runtime, runtime),
            force=force,
        )
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
    written = create_agent(target, agent_name=name, template=cast(Template, template))
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


@cli.command("eval")
@click.option(
    "--dataset",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--agent",
    "agent_spec",
    default="agent.py:answer",
    show_default=True,
    help="Agent entry point as path:function.",
)
@click.option(
    "--report",
    "report_fmt",
    type=click.Choice(["json", "html", "junit"]),
    default="json",
)
@click.option("--threshold", type=float, default=0.7, show_default=True)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write report to this file (otherwise stdout).",
)
def eval_cmd(
    dataset: Path,
    agent_spec: str,
    report_fmt: str,
    threshold: float,
    output: Path | None,
) -> None:
    """Run a golden-set eval over the project's agent."""
    report, rendered = asyncio.run(
        run_eval(
            dataset=dataset,
            agent_spec=agent_spec,
            threshold=threshold,
            report_fmt=report_fmt,
            output=output,
        )
    )
    if output is None:
        click.echo(rendered)
    else:
        click.echo(
            f"Wrote {report_fmt} report to {output} "
            f"(passed {report.passed_count}/{report.passed_count + report.failed_count})"
        )
    if report.failed_count > 0:
        raise click.exceptions.Exit(1)


@cli.command("deploy")
@click.option("--runtime", type=click.Choice(["aws", "gcp"]), required=True)
@click.option("--bucket", default=None, help="S3 bucket for AWS uploads.")
@click.option("--service", default="eap-agent", help="Cloud Run service name.")
@click.option("--dry-run", is_flag=True, help="Show plan, write nothing.")
def deploy_cmd(runtime: str, bucket: str | None, service: str, dry_run: bool) -> None:
    """Package the project for AWS or GCP deployment."""
    project = Path.cwd()
    if dry_run:
        click.echo(f"[dry-run] would package {runtime} target for {project}")
        return
    if runtime == "aws":
        zip_path = package_aws(project)
        click.echo(f"Packaged: {zip_path}")
        if bucket:
            if _real_deploy_enabled():
                where = upload_aws(zip_path, bucket)
                click.echo(f"Uploaded: {where}")
            else:
                click.echo(
                    f"Set EAP_ENABLE_REAL_DEPLOY=1 to upload. "
                    f"Otherwise: aws s3 cp {zip_path} s3://{bucket}/"
                )
        else:
            click.echo(f"Use: aws s3 cp {zip_path} s3://<bucket>/")
    else:
        target = package_gcp(project, service=service)
        click.echo(f"Packaged: {target}")
        if _real_deploy_enabled():
            where = deploy_gcp(target, service)
            click.echo(f"Deployed: {where}")
        else:
            click.echo(
                f"Set EAP_ENABLE_REAL_DEPLOY=1 to deploy. "
                f"Otherwise: gcloud run deploy {service} --source {target}"
            )


if __name__ == "__main__":
    cli()
