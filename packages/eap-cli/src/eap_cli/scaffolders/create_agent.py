"""`eap create-agent` scaffolder."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from eap_cli.scaffolders.render import render_template_dir

Template = Literal["research", "transactional"]
_VALID_TEMPLATES: set[str] = {"research", "transactional"}


def _templates_root() -> Path:
    return Path(__file__).resolve().parents[1] / "templates"


def create_agent(
    target: Path,
    *,
    agent_name: str,
    template: Template,
    force: bool = True,
) -> list[Path]:
    """Overlay agent template files into `target`.

    `force=True` by default — `create-agent` is meant to overwrite the
    `agent.py` and any tools that the template ships.
    """
    if template not in _VALID_TEMPLATES:
        raise ValueError(f"unknown template {template!r}; valid: {sorted(_VALID_TEMPLATES)}")
    src = _templates_root() / template
    return render_template_dir(src, target, {"agent_name": agent_name}, force=force)
