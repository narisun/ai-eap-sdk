"""Coverage for eap-cli's ``_version.py`` helper + fallback path.

Mirror of ``packages/eap-core/tests/test_version_fallback.py`` — both
packages share the same ``_version_from_pyproject(path: Path)`` shape
so the tests exercise the same branches.
"""

from __future__ import annotations

from pathlib import Path

from eap_cli._version import _version_from_pyproject


def test_version_from_pyproject_returns_string_for_well_formed_file(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\nversion = "9.8.7"\n', encoding="utf-8")
    assert _version_from_pyproject(pyproject) == "9.8.7"


def test_version_from_pyproject_returns_none_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    assert _version_from_pyproject(missing) is None


def test_version_from_pyproject_returns_none_when_version_key_absent(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_version_from_pyproject_returns_none_when_project_table_absent(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[build-system]\nrequires = []\n", encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_version_from_pyproject_returns_none_on_malformed_toml(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("garbage = [not valid\n", encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_version_from_pyproject_returns_none_when_version_is_not_string(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\nversion = 7\n', encoding="utf-8")
    assert _version_from_pyproject(pyproject) is None


def test_module_version_string_resolves_to_some_value() -> None:
    import eap_cli
    from eap_cli import _version

    assert isinstance(_version.__version__, str)
    assert _version.__version__
    assert _version.__version__ == eap_cli.__version__
    assert _version.__version__ != "unknown"
