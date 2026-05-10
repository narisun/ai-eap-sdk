# packages/eap-cli/tests/test_create_agent.py
from pathlib import Path

from click.testing import CliRunner

from eap_cli.main import cli


def _init_and_create(tmp_path: Path, template: str, agent_name: str = "myagent") -> Path:
    runner = CliRunner()
    target = tmp_path / "demo"
    runner.invoke(cli, ["init", str(target), "--name", "demo"])
    result = runner.invoke(
        cli,
        ["create-agent", "--name", agent_name, "--template", template],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return target


def test_create_research_agent_overlays_files(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    target = tmp_path / "demo"
    runner.invoke(cli, ["init", str(target), "--name", "demo"])
    monkeypatch.chdir(target)
    result = runner.invoke(cli, ["create-agent", "--name", "researcher", "--template", "research"])
    assert result.exit_code == 0, result.output
    assert (target / "agent.py").read_text().count("research") >= 1
    assert (target / "tools" / "search_docs.py").is_file()


def test_create_agent_unknown_template_errors(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    target = tmp_path / "demo"
    runner.invoke(cli, ["init", str(target), "--name", "demo"])
    monkeypatch.chdir(target)
    result = runner.invoke(cli, ["create-agent", "--name", "x", "--template", "bogus"])
    assert result.exit_code != 0
