"""`eap create-tool` scaffolder."""
from __future__ import annotations

from pathlib import Path

from eap_cli.scaffolders.render import render_template_dir


def _templates_root() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def create_tool(
    target: Path,
    *,
    name: str,
    requires_auth: bool = False,
) -> list[Path]:
    src = _templates_root() / "tool"
    return render_template_dir(
        src,
        target,
        {"name": name, "requires_auth": "True" if requires_auth else "False"},
        force=True,
    )
