"""Top-level Click app for `eap`."""
from __future__ import annotations

import click


@click.group()
@click.version_option()
def cli() -> None:
    """EAP-Core CLI — scaffold and operate agentic AI projects."""


if __name__ == "__main__":
    cli()
