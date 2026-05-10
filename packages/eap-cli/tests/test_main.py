from click.testing import CliRunner

from eap_cli.main import cli


def test_eap_help_runs():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "scaffold" in result.output.lower() or "EAP-Core" in result.output


def test_eap_version_prints_a_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert any(c.isdigit() for c in result.output)
