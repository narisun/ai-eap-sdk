"""Top-level Click app for `eap`."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import cast

import click

from eap_cli.scaffolders.create_agent import Template, create_agent
from eap_cli.scaffolders.create_mcp_server import create_mcp_server
from eap_cli.scaffolders.create_tool import create_tool
from eap_cli.scaffolders.deploy import (
    _real_deploy_enabled,
    deploy_agentcore,
    deploy_gcp,
    deploy_vertex_agent_engine,
    package_agentcore,
    package_aws,
    package_gcp,
    package_vertex_agent_engine,
    upload_aws,
)
from eap_cli.scaffolders.eval_cmd import run_eval
from eap_cli.scaffolders.init import Runtime, init_project
from eap_cli.scaffolders.publish_gateway import publish_to_gateway


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


@cli.command("create-mcp-server")
@click.argument("target", type=click.Path(file_okay=False, path_type=Path))
@click.option("--name", default=None, help="Server name (defaults to target dir name).")
@click.option("--force", is_flag=True, help="Overwrite existing files.")
def create_mcp_server_cmd(target: Path, name: str | None, force: bool) -> None:
    """Scaffold a standalone MCP-stdio server project (no LLM agent)."""
    server_name = name or target.name
    try:
        written = create_mcp_server(target, server_name=server_name, force=force)
    except FileExistsError as e:
        raise click.ClickException(f"{e}. Re-run with --force to overwrite.") from e
    click.echo(f"Wrote {len(written)} files for MCP server {server_name!r} to {target}")


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
@click.option(
    "--runtime",
    type=click.Choice(["aws", "gcp", "agentcore", "vertex-agent-engine"]),
    required=True,
)
@click.option("--bucket", default=None, help="S3 bucket for AWS uploads.")
@click.option("--service", default="eap-agent", help="Cloud Run / AgentCore name.")
@click.option(
    "--entry",
    default="agent.py:answer",
    show_default=True,
    help="Agent entry point as path:function (used by --runtime agentcore).",
)
@click.option(
    "--region",
    default="us-east-1",
    show_default=True,
    help="AWS region (used by --runtime agentcore).",
)
@click.option(
    "--auth-discovery-url",
    default=None,
    help="OIDC discovery URL for InboundJwtVerifier.",
)
@click.option(
    "--auth-issuer",
    default=None,
    help="Expected issuer (`iss`) claim.",
)
@click.option(
    "--auth-audience",
    "auth_audiences",
    multiple=True,
    help="Allowed audience(s). Repeat for multiple.",
)
@click.option(
    "--allow-unauthenticated",
    is_flag=True,
    help="Skip auth wiring — only for non-production.",
)
@click.option("--dry-run", is_flag=True, help="Show plan, write nothing.")
def deploy_cmd(
    runtime: str,
    bucket: str | None,
    service: str,
    entry: str,
    region: str,
    auth_discovery_url: str | None,
    auth_issuer: str | None,
    auth_audiences: tuple[str, ...],
    allow_unauthenticated: bool,
    dry_run: bool,
) -> None:
    """Package the project for AWS, GCP, or AgentCore Runtime deployment."""
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
    elif runtime == "gcp":
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
    elif runtime == "agentcore":
        auth = _resolve_handler_auth(
            auth_discovery_url, auth_issuer, auth_audiences, allow_unauthenticated
        )
        target = package_agentcore(project, entry=entry, auth=auth)
        click.echo(f"Packaged: {target}")
        if _real_deploy_enabled():
            image = deploy_agentcore(target, name=service, region=region)
            click.echo(f"Built image: {image}")
            click.echo(f"Push to ECR and register with AgentCore Runtime — see {target}/README.md")
        else:
            click.echo(
                f"Set EAP_ENABLE_REAL_DEPLOY=1 to build the image locally. "
                f"Otherwise see {target}/README.md for build/push/register steps."
            )
    else:  # vertex-agent-engine
        auth = _resolve_handler_auth(
            auth_discovery_url, auth_issuer, auth_audiences, allow_unauthenticated
        )
        target = package_vertex_agent_engine(project, entry=entry, auth=auth)
        click.echo(f"Packaged: {target}")
        if _real_deploy_enabled():
            image = deploy_vertex_agent_engine(
                target,
                name=service,
                project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
                region=region,
            )
            click.echo(f"Built image: {image}")
            click.echo(
                f"Push to Artifact Registry and register with Vertex Agent Engine — "
                f"see {target}/README.md"
            )
        else:
            click.echo(
                f"Set EAP_ENABLE_REAL_DEPLOY=1 to build the image locally. "
                f"Otherwise see {target}/README.md for build/push/register steps."
            )


def _resolve_handler_auth(
    discovery_url: str | None,
    issuer: str | None,
    audiences: tuple[str, ...],
    allow_unauthenticated: bool,
) -> dict[str, object] | None:
    """Validate the --auth-* flags and return the auth dict (or None).

    Raises ``click.ClickException`` when:
      * neither a complete auth triple nor ``--allow-unauthenticated`` was passed,
      * a partial auth triple was passed (names the missing flags), or
      * a complete auth triple was combined with ``--allow-unauthenticated``
        (contradictory intent).

    Emits a loud warning to stderr when ``--allow-unauthenticated`` is used.
    """
    auth_configured = bool(discovery_url and issuer and audiences)
    missing: list[str] = []
    if not discovery_url:
        missing.append("--auth-discovery-url")
    if not issuer:
        missing.append("--auth-issuer")
    if not audiences:
        missing.append("--auth-audience")
    any_auth_set = len(missing) < 3

    if auth_configured and allow_unauthenticated:
        raise click.ClickException(
            "--allow-unauthenticated cannot be combined with --auth-* flags; pick one."
        )
    if not auth_configured and not allow_unauthenticated:
        if any_auth_set:
            raise click.ClickException(
                f"Incomplete auth configuration. Missing: {', '.join(missing)}. "
                f"Pass all three of --auth-discovery-url + --auth-issuer + "
                f"--auth-audience, or --allow-unauthenticated."
            )
        raise click.ClickException(
            "Deploy refuses to scaffold an unauthenticated handler. Pass "
            "--auth-discovery-url + --auth-issuer + --auth-audience (one or more), "
            "or --allow-unauthenticated to opt in explicitly (NOT for production)."
        )
    if not auth_configured:
        click.echo(
            "WARNING: scaffolding an unauthenticated handler. Do NOT use in production.",
            err=True,
        )
        return None
    return {
        "discovery_url": discovery_url,
        "issuer": issuer,
        "audiences": list(audiences),
    }


@cli.command("publish-to-gateway")
@click.option(
    "--entry",
    default="agent.py",
    show_default=True,
    help="Project entry file whose import side-effects register tools.",
)
@click.option(
    "--title",
    default=None,
    help="OpenAPI title (defaults to current directory name).",
)
@click.option(
    "--server-url",
    default="https://your-agent-host.example",
    show_default=True,
    help="Server URL recorded in the OpenAPI 'servers' field.",
)
@click.option("--dry-run", is_flag=True, help="Show plan, write nothing.")
def publish_gateway_cmd(
    entry: str,
    title: str | None,
    server_url: str,
    dry_run: bool,
) -> None:
    """Generate an OpenAPI spec from local @mcp_tools for AgentCore Gateway."""
    project = Path.cwd()
    if dry_run:
        click.echo(f"[dry-run] would publish {project} tools as OpenAPI spec")
        return
    target = publish_to_gateway(project, entry=entry, title=title, server_url=server_url)
    click.echo(f"Wrote OpenAPI spec to {target}/openapi.json")
    click.echo(f"Upload + register steps in {target}/README.md")


if __name__ == "__main__":
    cli()
