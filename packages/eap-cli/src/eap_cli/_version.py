"""Single source of truth for eap-cli's version.

See packages/eap-core/src/eap_core/_version.py for rationale.

Reads from ``pyproject.toml::project.version`` when running from a
source tree (always fresh — survives `pyproject.toml` bumps without
reinstalling). Falls back to installed package metadata via
`importlib.metadata.version()` when running from an installed wheel
(end-user case). Final fallback to ``"unknown"`` only if neither
mechanism resolves.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version_from_source_tree() -> str | None:
    """Return pyproject.toml::project.version if running from source.

    `_version.py` lives at packages/eap-cli/src/eap_cli/_version.py;
    `parents[2]` is `packages/eap-cli/` for source trees. For wheel
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
        __version__ = version("eap-cli")
    except PackageNotFoundError:
        __version__ = "unknown"
