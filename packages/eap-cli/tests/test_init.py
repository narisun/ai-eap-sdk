from pathlib import Path

import pytest
from click.testing import CliRunner

from eap_cli.main import cli


def test_eap_init_creates_runnable_project(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "demo-agent"
    result = runner.invoke(cli, ["init", str(target), "--name", "demo-agent", "--runtime", "local"])
    assert result.exit_code == 0, result.output

    expected = {
        "pyproject.toml", "agent.py",
        "tools/example_tool.py",
        "configs/policy.json", "configs/agent_card.json",
        "tests/golden_set.json", "responses.yaml",
        ".claude.md", ".gitignore", "README.md",
    }
    actual = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert expected.issubset(actual)

    pyproject_text = (target / "pyproject.toml").read_text()
    assert 'name = "demo-agent"' in pyproject_text


def test_eap_init_refuses_to_overwrite_without_force(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "x"
    runner.invoke(cli, ["init", str(target), "--name", "x"])
    result = runner.invoke(cli, ["init", str(target), "--name", "x"])
    assert result.exit_code != 0
    assert "force" in result.output.lower() or "exist" in result.output.lower()


def test_eap_init_force_overwrites(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "x"
    runner.invoke(cli, ["init", str(target), "--name", "x"])
    result = runner.invoke(cli, ["init", str(target), "--name", "x-new", "--force"])
    assert result.exit_code == 0
    assert 'name = "x-new"' in (target / "pyproject.toml").read_text()


def test_eap_init_runtime_propagates_to_agent(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "x"
    runner.invoke(cli, ["init", str(target), "--name", "x", "--runtime", "local"])
    agent_text = (target / "agent.py").read_text()
    assert 'provider="local"' in agent_text
