"""End-to-end CLI test: scaffold a project, run agent.py as a subprocess, eval."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from eap_cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_scaffold_then_run_agent_subprocess(tmp_path: Path, runner):
    target = tmp_path / "demo"
    res = runner.invoke(cli, ["init", str(target), "--name", "demo", "--runtime", "local"])
    assert res.exit_code == 0

    completed = subprocess.run(
        [sys.executable, str(target / "agent.py")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    out = completed.stdout
    assert "Hello back" in out or "[local-runtime]" in out


def test_scaffold_transactional_then_run_subprocess(tmp_path: Path, runner, monkeypatch):
    target = tmp_path / "bank"
    runner.invoke(cli, ["init", str(target), "--name", "bank", "--runtime", "local"])
    monkeypatch.chdir(target)
    runner.invoke(cli, ["create-agent", "--name", "bank", "--template", "transactional"])

    completed = subprocess.run(
        [sys.executable, str(target / "agent.py")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0
    out = completed.stdout
    # The transactional template's run() executes a transfer and prints the result dict.
    assert "status" in out
    assert "ok" in out
    assert "amount_cents" in out


def test_scaffold_research_then_eval_subprocess(tmp_path: Path, runner, monkeypatch):
    target = tmp_path / "research"
    runner.invoke(cli, ["init", str(target), "--name", "research", "--runtime", "local"])
    monkeypatch.chdir(target)
    runner.invoke(cli, ["create-agent", "--name", "research", "--template", "research"])

    out = target / "report.json"
    res = runner.invoke(
        cli,
        [
            "eval",
            "--dataset",
            "tests/golden_set.json",
            "--agent",
            "agent.py:answer",
            "--report",
            "json",
            "--output",
            str(out),
            "--threshold",
            "0.0",
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(out.read_text())
    assert "aggregate" in data
