"""Coverage for `_version.py`'s helper + wheel-install fallback path.

The module exposes ``_version_from_pyproject(path: Path)`` as a private
helper specifically so the resolution branches (file missing, malformed
TOML, missing ``version`` key, non-string version value, happy path)
are directly testable without monkeypatching ``Path``.

These tests assert behavior against synthetic ``pyproject.toml`` files
written under ``tmp_path`` — exactly the shape the helper is designed
for. They complement ``test_version.py`` which pins the resolved
``__version__`` to the workspace's real ``pyproject.toml``.
"""

from __future__ import annotations

from pathlib import Path

from eap_core._version import _version_from_pyproject


def test_version_from_pyproject_returns_string_for_well_formed_file(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\nversion = "1.2.3"\n', encoding="utf-8")
    assert _version_from_pyproject(pyproject) == "1.2.3"


def test_version_from_pyproject_returns_none_when_file_missing(tmp_path: Path) -> None:
    """Wheel-install path: no ``pyproject.toml`` at the expected location."""
    missing = tmp_path / "does_not_exist.toml"
    assert _version_from_pyproject(missing) is None


def test_version_from_pyproject_returns_none_when_version_key_absent(tmp_path: Path) -> None:
    """A ``[project]`` table without a ``version`` key falls back (not raise)."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_version_from_pyproject_returns_none_when_project_table_absent(tmp_path: Path) -> None:
    """No ``[project]`` table at all — KeyError on ``data["project"]``."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[build-system]\nrequires = []\n", encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_version_from_pyproject_returns_none_on_malformed_toml(tmp_path: Path) -> None:
    """``tomllib.TOMLDecodeError`` must not propagate."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("this is not = [valid toml\n", encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_version_from_pyproject_returns_none_when_version_is_not_string(tmp_path: Path) -> None:
    """Defensive: a ``version = 1`` (int) entry should not pass through.

    The helper's last guard rejects non-string values rather than
    returning whatever shape was in the file — the public contract is
    ``str | None``.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\nversion = 1\n', encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_module_version_string_resolves_to_some_value() -> None:
    """``__version__`` resolves to a non-empty string in either path
    (source-tree or wheel). Pinning the exact value lives in
    ``test_version.py``; here we just confirm the module attribute is
    bound and not the ``"unknown"`` sentinel under normal install.
    """
    import eap_core
    from eap_core import _version

    assert isinstance(_version.__version__, str)
    assert _version.__version__
    assert _version.__version__ == eap_core.__version__
    # We're running from a source tree in CI/dev, so this must NOT
    # have hit the deepest fallback.
    assert _version.__version__ != "unknown"
