"""Regression tests for the ``eap publish-to-gateway`` scaffolder.

These guard the two silent-failure modes around registry resolution:

- empty registry (no tools after import) must hard-fail rather than
  produce a valid-looking zero-tool OpenAPI spec;
- ambiguous registry attribute names (both ``registry`` and ``REGISTRY``
  defined on the entry module) must hard-fail rather than silently pick
  one and diverge from the scaffolded convention.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from eap_cli.main import cli


def test_publish_gateway_refuses_empty_registry(tmp_path: Path, monkeypatch):
    """If the entry module exposes no tools, scaffolder must raise — not produce a zero-tool spec."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "agent.py").write_text("# no tools defined\n")
    (project / "pyproject.toml").write_text('[project]\nname="p"\nversion="0.1.0"\n')
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["publish-to-gateway", "--entry", "agent.py"])
    assert result.exit_code != 0
    assert "no tools found" in result.output or "no tools found" in str(result.exception)


def test_publish_gateway_refuses_both_registry_attrs(tmp_path: Path, monkeypatch):
    """If module exposes both `registry` and `REGISTRY`, scaffolder must raise."""
    project = tmp_path / "p"
    project.mkdir()
    (project / "agent.py").write_text(
        "from eap_core.mcp import McpToolRegistry\n"
        "registry = McpToolRegistry()\n"
        "REGISTRY = McpToolRegistry()\n"
    )
    (project / "pyproject.toml").write_text('[project]\nname="p"\nversion="0.1.0"\n')
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["publish-to-gateway", "--entry", "agent.py"])
    assert result.exit_code != 0
    assert "both `registry` and `REGISTRY`" in (result.output + str(result.exception))
