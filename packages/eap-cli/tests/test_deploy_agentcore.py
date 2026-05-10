"""Tests for `eap deploy --runtime agentcore` packaging."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from eap_cli.main import cli


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "agent.py").write_text(
        "async def answer(query: str) -> str:\n    return f'echo: {query}'\n"
    )
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    return project


def test_deploy_agentcore_writes_dockerfile_handler_and_readme(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "agentcore"])
    assert result.exit_code == 0, result.output

    target = project / "dist" / "agentcore"
    assert (target / "Dockerfile").is_file()
    assert (target / "handler.py").is_file()
    assert (target / "README.md").is_file()
    # User source should be staged alongside.
    assert (target / "agent.py").is_file()
    assert (target / "pyproject.toml").is_file()


def test_deploy_agentcore_dockerfile_is_arm64(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    runner.invoke(cli, ["deploy", "--runtime", "agentcore"])
    df = (project / "dist" / "agentcore" / "Dockerfile").read_text()
    assert "linux/arm64" in df
    assert "EXPOSE 8080" in df
    assert "fastapi" in df.lower()
    assert "uvicorn" in df.lower()


def test_deploy_agentcore_handler_implements_protocol_contract(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    runner.invoke(cli, ["deploy", "--runtime", "agentcore"])
    h = (project / "dist" / "agentcore" / "handler.py").read_text()
    # Routes the AgentCore HTTP protocol contract requires.
    assert "/invocations" in h
    assert "/ping" in h
    # The default entry must be wired in.
    assert "agent.py:answer" in h
    # Listens on the AgentCore-required host:port.
    assert "0.0.0.0" in h
    assert "8080" in h


def test_deploy_agentcore_custom_entry_propagates(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    (project / "main.py").write_text("def go(prompt): return prompt.upper()\n")
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "agentcore", "--entry", "main.py:go"])
    assert result.exit_code == 0, result.output
    h = (project / "dist" / "agentcore" / "handler.py").read_text()
    assert "main.py:go" in h


def test_deploy_agentcore_dry_run_writes_nothing(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "agentcore", "--dry-run"])
    assert result.exit_code == 0
    assert not (project / "dist").exists()


def test_deploy_agentcore_live_blocked_without_env_flag(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv("EAP_ENABLE_REAL_DEPLOY", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "agentcore", "--service", "my-agent"])
    assert result.exit_code == 0
    assert "EAP_ENABLE_REAL_DEPLOY" in result.output
    # Package was created but no docker build attempted.
    assert (project / "dist" / "agentcore" / "Dockerfile").is_file()


def test_handler_runs_via_asgi_when_fastapi_installed(tmp_path: Path, monkeypatch):
    """End-to-end: scaffold an agent, package it, import handler.py,
    and exercise /ping + /invocations through ASGI."""
    import importlib.util
    import sys

    import pytest

    pytest.importorskip("fastapi")
    import httpx

    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    runner.invoke(cli, ["deploy", "--runtime", "agentcore"])

    target = project / "dist" / "agentcore"
    # Load the generated handler.py as a module.
    spec = importlib.util.spec_from_file_location("agentcore_handler", target / "handler.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["agentcore_handler"] = module
    monkeypatch.chdir(target)
    spec.loader.exec_module(module)

    transport = httpx.ASGITransport(app=module.app)
    import asyncio

    async def _exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            ping = await ac.get("/ping")
            assert ping.status_code == 200
            assert ping.json()["status"] == "Healthy"
            assert "time_of_last_update" in ping.json()

            inv = await ac.post("/invocations", json={"prompt": "hi"})
            assert inv.status_code == 200, inv.text
            body = inv.json()
            assert body["status"] == "success"
            assert body["response"] == "echo: hi"

    asyncio.run(_exercise())
