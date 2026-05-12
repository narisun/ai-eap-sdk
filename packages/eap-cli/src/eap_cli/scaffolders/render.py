"""Jinja2 template rendering for scaffolded projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined


def _env() -> Environment:
    # nosec B701 — autoescape=False is intentional. The scaffolder renders
    # Python source, YAML, TOML, and Dockerfile templates (NOT HTML), where
    # HTML entity escaping (e.g. ``&`` → ``&amp;``) would corrupt the
    # generated code. The template inputs come from the developer running
    # the CLI on their own machine, not from untrusted network input, so
    # the XSS threat model B701 targets does not apply here.
    return Environment(
        keep_trailing_newline=True,
        undefined=StrictUndefined,
        autoescape=False,  # nosec B701 - generating code, not HTML; see comment above
    )


def _maybe_substitute_name(path: Path, variables: dict[str, Any]) -> Path:
    if "name" not in variables:
        return path
    parts = []
    for part in path.parts:
        if part == "__name__":
            parts.append(str(variables["name"]))
        elif part.startswith("__name__."):
            parts.append(str(variables["name"]) + part[len("__name__") :])
        else:
            parts.append(part)
    return Path(*parts)


def render_template_dir(
    src: Path,
    dst: Path,
    variables: dict[str, Any],
    *,
    force: bool = False,
) -> list[Path]:
    """Render all `*.j2` files under `src` into `dst`, dropping the `.j2` suffix.

    `template.toml` is treated as metadata and not copied. Returns the list of
    files written.
    """
    env = _env()
    written: list[Path] = []
    for src_file in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = src_file.relative_to(src)
        if rel.name == "template.toml":
            continue
        if rel.suffix != ".j2":
            target = dst / rel
            content = src_file.read_text()
        else:
            target = dst / rel.with_suffix("")
            content = env.from_string(src_file.read_text()).render(**variables)
        target = _maybe_substitute_name(target, variables)
        if target.exists() and not force:
            raise FileExistsError(f"{target} already exists; pass force=True to overwrite")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(target)
    return written
