"""Tests for `eap deploy --runtime vertex-agent-engine`."""

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


def test_deploy_vertex_writes_dockerfile_handler_and_readme(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "vertex-agent-engine"])
    assert result.exit_code == 0, result.output

    target = project / "dist" / "vertex-agent-engine"
    assert (target / "Dockerfile").is_file()
    assert (target / "handler.py").is_file()
    assert (target / "README.md").is_file()
    assert (target / "agent.py").is_file()
    assert (target / "pyproject.toml").is_file()


def test_deploy_vertex_dockerfile_targets_amd64_and_cloud_run_port(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    runner.invoke(cli, ["deploy", "--runtime", "vertex-agent-engine"])
    df = (project / "dist" / "vertex-agent-engine" / "Dockerfile").read_text()
    assert "linux/amd64" in df
    assert "PORT=8080" in df
    assert "EXPOSE 8080" in df


def test_deploy_vertex_handler_exposes_invocations_and_health(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    runner.invoke(cli, ["deploy", "--runtime", "vertex-agent-engine"])
    h = (project / "dist" / "vertex-agent-engine" / "handler.py").read_text()
    assert "/invocations" in h
    assert "/health" in h
    assert "agent.py:answer" in h
    assert "0.0.0.0" in h
    # PORT env var honored per Cloud Run convention
    assert "PORT" in h


def test_deploy_vertex_custom_entry_propagates(tmp_path, monkeypatch):
    project = _project(tmp_path)
    (project / "main.py").write_text("def go(p): return p.upper()\n")
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["deploy", "--runtime", "vertex-agent-engine", "--entry", "main.py:go"],
    )
    assert result.exit_code == 0
    h = (project / "dist" / "vertex-agent-engine" / "handler.py").read_text()
    assert "main.py:go" in h


def test_deploy_vertex_dry_run_writes_nothing(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "vertex-agent-engine", "--dry-run"])
    assert result.exit_code == 0
    assert not (project / "dist").exists()


def test_deploy_vertex_live_blocked_without_env_flag(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv("EAP_ENABLE_REAL_DEPLOY", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["deploy", "--runtime", "vertex-agent-engine", "--service", "my-agent"],
    )
    assert result.exit_code == 0
    assert "EAP_ENABLE_REAL_DEPLOY" in result.output
    # Package was created but no docker build attempted.
    assert (project / "dist" / "vertex-agent-engine" / "Dockerfile").is_file()


def test_handler_runs_via_asgi_when_fastapi_installed(tmp_path: Path, monkeypatch):
    """End-to-end smoke: scaffold, package, exercise /health + /invocations."""
    import asyncio
    import importlib.util
    import sys

    import pytest

    pytest.importorskip("fastapi")
    import httpx

    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    runner.invoke(cli, ["deploy", "--runtime", "vertex-agent-engine"])

    target = project / "dist" / "vertex-agent-engine"
    spec = importlib.util.spec_from_file_location("vertex_handler", target / "handler.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["vertex_handler"] = module
    monkeypatch.chdir(target)
    spec.loader.exec_module(module)

    transport = httpx.ASGITransport(app=module.app)

    async def _exercise() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            health = await ac.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "OK"

            inv = await ac.post("/invocations", json={"prompt": "hi"})
            assert inv.status_code == 200, inv.text
            body = inv.json()
            assert body["status"] == "success"
            assert body["response"] == "echo: hi"

    asyncio.run(_exercise())
