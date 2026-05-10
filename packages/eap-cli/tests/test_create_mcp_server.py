from pathlib import Path

from click.testing import CliRunner
from eap_cli.main import cli


def test_create_mcp_server_writes_runnable_skeleton(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "my-mcp"
    result = runner.invoke(cli, ["create-mcp-server", str(target), "--name", "my-mcp"])
    assert result.exit_code == 0, result.output

    expected = {
        "pyproject.toml",
        "server.py",
        "tools/__init__.py",
        "tools/example_tool.py",
        "configs/agent_card.json",
        ".claude.md",
        ".gitignore",
        "README.md",
    }
    actual = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()}
    assert expected.issubset(actual), f"missing: {expected - actual}"

    # server.py must reference the user's server name and run_stdio
    server_text = (target / "server.py").read_text()
    assert "my-mcp" in server_text
    assert "run_stdio" in server_text
    assert "default_registry" in server_text

    # pyproject must depend on the [mcp] extra
    py_text = (target / "pyproject.toml").read_text()
    assert "eap-core[mcp]" in py_text


def test_create_mcp_server_refuses_overwrite_without_force(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "x"
    runner.invoke(cli, ["create-mcp-server", str(target), "--name", "x"])
    result = runner.invoke(cli, ["create-mcp-server", str(target), "--name", "x"])
    assert result.exit_code != 0
    assert "force" in result.output.lower() or "exist" in result.output.lower()


def test_create_mcp_server_force_overwrites(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "x"
    runner.invoke(cli, ["create-mcp-server", str(target), "--name", "x"])
    result = runner.invoke(cli, ["create-mcp-server", str(target), "--name", "y", "--force"])
    assert result.exit_code == 0
    assert "y" in (target / "server.py").read_text()


def test_create_mcp_server_defaults_name_to_target_dir(tmp_path: Path):
    runner = CliRunner()
    target = tmp_path / "auto-named-server"
    result = runner.invoke(cli, ["create-mcp-server", str(target)])
    assert result.exit_code == 0
    assert "auto-named-server" in (target / "server.py").read_text()
