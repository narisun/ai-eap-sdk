# packages/eap-cli/tests/test_eval_cmd.py
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from eap_cli.main import cli
from eap_cli.scaffolders.eval_cmd import _load_callable, _make_agent, render_report

from eap_core.eval.runner import EvalCase, EvalReport

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


# ---- _load_callable error / branch coverage --------------------------------


def test_load_callable_rejects_spec_without_colon():
    """Covers eval_cmd.py:26 — agent spec missing the ``:func`` half."""
    with pytest.raises(ValueError, match="path:function"):
        _load_callable("no_colon_here")


def test_load_callable_raises_when_spec_cannot_be_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Covers eval_cmd.py:36 — ``spec_from_file_location`` returning None
    must surface as ImportError naming the target file. The cpython stdlib
    only returns None for unrecognized file types, so we force it via
    monkeypatching to exercise the defensive guard.
    """
    import importlib.util as importlib_util

    target = tmp_path / "agent.py"
    target.write_text("answer = 1\n")
    monkeypatch.setattr(importlib_util, "spec_from_file_location", lambda *a, **kw: None)
    with pytest.raises(ImportError, match=r"agent\.py"):
        _load_callable(f"{target}:answer")


def test_load_callable_raises_when_module_has_no_attribute(tmp_path: Path):
    """Covers eval_cmd.py:43 — ``getattr`` miss raises AttributeError naming
    the missing attribute and target."""
    target = tmp_path / "tiny_agent.py"
    target.write_text("def something_else(): pass\n")
    with pytest.raises(AttributeError, match="nonexistent"):
        _load_callable(f"{target}:nonexistent")


def test_load_callable_loads_module_path_form():
    """Covers eval_cmd.py:41 — when the spec's left side has no ``.py``
    suffix we fall through to ``importlib.import_module`` instead of the
    file-loader branch. ``json`` is a stdlib module that ships with
    ``loads``, so the load succeeds without disk-touching."""
    fn = _load_callable("json:loads")
    # The loader actually returns the resolved callable.
    assert callable(fn)
    assert fn('"hi"') == "hi"


# ---- _make_agent non-coroutine branch (eval_cmd.py:54) ---------------------


async def test_make_agent_handles_synchronous_callable():
    """Covers eval_cmd.py:54 — when the user's ``answer`` is a regular
    function (not async) the wrapper takes the ``else`` branch and uses
    the return value directly instead of awaiting it.
    """

    def sync_answer(prompt: str) -> str:
        return f"echoed: {prompt}"

    agent = _make_agent(sync_answer)
    case = EvalCase(id="c1", input="hello", expected_contexts=["x"])
    traj = await agent(case)
    assert traj.final_answer == "echoed: hello"
    assert traj.request_id == "c1"
    assert traj.retrieved_contexts == ["x"]


# ---- render_report format branches (eval_cmd.py:68-72) ----------------------


def _empty_report() -> EvalReport:
    return EvalReport(cases=[], aggregate={}, threshold=0.7, passed_count=0, failed_count=0)


def test_render_report_emits_html():
    """Covers eval_cmd.py:68-69 — HTML branch goes through ``emit_html``."""
    rendered = render_report(_empty_report(), "html")
    # HTML emitter is expected to produce real HTML markup.
    assert "<" in rendered and ">" in rendered


def test_render_report_emits_junit():
    """Covers eval_cmd.py:70-71 — JUnit branch goes through ``emit_junit``."""
    rendered = render_report(_empty_report(), "junit")
    # JUnit XML must include the testsuite element.
    assert "<testsuite" in rendered or "<testsuites" in rendered


def test_render_report_rejects_unknown_format():
    """Covers eval_cmd.py:72 — unknown report format raises ValueError
    naming the bad format so the caller knows what failed."""
    with pytest.raises(ValueError, match=r"yaml"):
        render_report(_empty_report(), "yaml")


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
