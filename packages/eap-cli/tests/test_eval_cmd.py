# packages/eap-cli/tests/test_eval_cmd.py
import json
from pathlib import Path

from click.testing import CliRunner
from eap_cli.main import cli

_AGENT_PY = '''
async def answer(query: str) -> str:
    """Trivial agent for tests: returns a fixed support string."""
    return "Paris is the capital of France."
'''


def _setup(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "agent.py").write_text(_AGENT_PY)
    (project / "golden.json").write_text(
        json.dumps(
            [
                {
                    "id": "c1",
                    "input": "What is the capital?",
                    "expected_contexts": ["Paris is the capital of France."],
                },
            ]
        )
    )
    return project


def test_eval_command_runs_agent_and_emits_json(tmp_path: Path, monkeypatch):
    project = _setup(tmp_path)
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["eval", "--dataset", "golden.json", "--report", "json", "--threshold", "0.5"],
    )
    assert result.exit_code == 0, result.output
    json.loads(
        result.output
        if result.output.strip().startswith("{")
        else result.output.strip().splitlines()[-1].strip()
    )


def test_eval_command_writes_report_file_when_output_passed(tmp_path: Path, monkeypatch):
    project = _setup(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    out = project / "report.json"
    result = runner.invoke(
        cli,
        [
            "eval",
            "--dataset",
            "golden.json",
            "--report",
            "json",
            "--output",
            str(out),
            "--threshold",
            "0.5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.is_file()
    parsed = json.loads(out.read_text())
    assert parsed["aggregate"]["faithfulness"] >= 0


def test_eval_command_exits_nonzero_when_below_threshold(tmp_path: Path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    (project / "agent.py").write_text(
        'async def answer(q: str) -> str:\n    return "Mars unicorns nonsense."\n'
    )
    (project / "golden.json").write_text(
        json.dumps(
            [
                {
                    "id": "c1",
                    "input": "anything",
                    "expected_contexts": ["Paris is the capital of France."],
                },
            ]
        )
    )
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["eval", "--dataset", "golden.json", "--report", "json", "--threshold", "0.7"],
    )
    assert result.exit_code != 0
