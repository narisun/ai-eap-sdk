from pathlib import Path

from click.testing import CliRunner

from eap_cli.main import cli


def test_create_tool_writes_typed_stub(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    target = tmp_path / "demo"
    runner.invoke(cli, ["init", str(target), "--name", "demo"])
    monkeypatch.chdir(target)
    result = runner.invoke(cli, ["create-tool", "--name", "lookup_account", "--mcp"])
    assert result.exit_code == 0, result.output
    tool_file = target / "tools" / "lookup_account.py"
    assert tool_file.is_file()
    body = tool_file.read_text()
    assert "@mcp_tool" in body
    assert "async def lookup_account" in body
    assert "default_registry().register" in body


def test_create_tool_with_auth_required_marks_spec(tmp_path: Path, monkeypatch):
    runner = CliRunner()
    target = tmp_path / "demo"
    runner.invoke(cli, ["init", str(target), "--name", "demo"])
    monkeypatch.chdir(target)
    runner.invoke(cli, ["create-tool", "--name", "do_write", "--mcp", "--auth-required"])
    body = (target / "tools" / "do_write.py").read_text()
    assert "requires_auth=True" in body
