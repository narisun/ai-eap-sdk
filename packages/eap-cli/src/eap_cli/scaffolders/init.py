"""`eap init` scaffolder."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from eap_cli.scaffolders.render import render_template_dir

Runtime = Literal["local", "bedrock", "vertex"]


def _templates_root() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def init_project(
    target: Path,
    *,
    project_name: str,
    runtime: Runtime = "local",
    force: bool = False,
) -> list[Path]:
    src = _templates_root() / "init"
    return render_template_dir(
        src,
        target,
        {"project_name": project_name, "runtime": runtime},
        force=force,
    )
