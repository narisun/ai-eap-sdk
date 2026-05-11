"""Single source of truth for eap-core's version.

Reads from ``pyproject.toml::project.version`` when running from a
source tree (always fresh — survives `pyproject.toml` bumps without
reinstalling). Falls back to installed package metadata via
`importlib.metadata.version()` when running from an installed wheel
(end-user case). Final fallback to ``"unknown"`` only if neither
mechanism resolves.

The order is deliberate: source-tree wins because in dev environments
the editable install's METADATA can go stale between bumps and
``uv sync`` calls (see v0.5.1 review finding L1). Reading
``pyproject.toml`` directly closes that gap.

Wheel installs (the end-user path) don't ship ``pyproject.toml`` in
the installed layout, so the source-tree probe fails harmlessly and
``importlib.metadata.version()`` is authoritative — which is what
wheel users want.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version_from_source_tree() -> str | None:
    """Return pyproject.toml::project.version if running from source.

    `_version.py` lives at packages/eap-core/src/eap_core/_version.py;
    `parents[2]` is `packages/eap-core/` for source trees. For wheel
    installs there's no pyproject.toml at that path.
    """
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        resolved = data["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None
    return resolved if isinstance(resolved, str) else None


_source_version = _version_from_source_tree()
if _source_version is not None:
    __version__: str = _source_version
else:
    try:
        # NOTE: only catch PackageNotFoundError (subclass of
        # ModuleNotFoundError → ImportError). Do not broaden — we want
        # stdlib import failures to surface.
        __version__ = version("eap-core")
    except PackageNotFoundError:
        __version__ = "unknown"
