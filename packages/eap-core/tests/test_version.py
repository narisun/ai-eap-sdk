"""Pin __version__ to pyproject.toml::project.version.

This is a workspace-only regression test — it asserts that the
SDK's __version__ exposed to consumers matches what `pyproject.toml`
declares. Skipping it on layout drift would defeat the lock; the
test fails hard if the expected layout isn't found.

Without this test, the two source-of-truth values can drift silently
across releases (as they did between v0.2.0 and v0.5.1 — see L1 of the
v0.5.1 pre-prod review).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _read_pyproject_version(package: str) -> str:
    pyproject = _REPO_ROOT / "packages" / package / "pyproject.toml"
    if not pyproject.is_file():
        pytest.fail(
            f"pyproject.toml for {package} not present at {pyproject} — "
            f"the version-drift regression test is workspace-only and "
            f"this file must exist for the assertion to be meaningful"
        )
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_eap_core_version_matches_pyproject():
    import eap_core

    expected = _read_pyproject_version("eap-core")
    assert eap_core.__version__ == expected, (
        f"eap_core.__version__ ({eap_core.__version__!r}) drifted from "
        f"pyproject.toml::project.version ({expected!r}). "
        f"Either bump _version.py or migrate to importlib.metadata."
    )


def test_eap_cli_version_matches_pyproject():
    import eap_cli

    expected = _read_pyproject_version("eap-cli")
    assert eap_cli.__version__ == expected, (
        f"eap_cli.__version__ ({eap_cli.__version__!r}) drifted from "
        f"pyproject.toml::project.version ({expected!r}). "
        f"Either bump _version.py or migrate to importlib.metadata."
    )
