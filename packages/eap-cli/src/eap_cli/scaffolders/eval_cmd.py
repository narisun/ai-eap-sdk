"""`eap eval` runner.

Loads the user's `agent.py:answer` (or configured target) dynamically and
drives the configured dataset through it. Uses
`eap_core.eval.EvalRunner` and the report emitters.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

from eap_core.eval.faithfulness import DeterministicJudge, FaithfulnessScorer
from eap_core.eval.reports import emit_html, emit_json, emit_junit
from eap_core.eval.runner import EvalCase, EvalReport, EvalRunner
from eap_core.eval.trajectory import Trajectory


def _load_callable(spec: str) -> Callable[..., Any]:
    """Load an entry point of the form `module_path.py:func` or `module:func`."""
    if ":" not in spec:
        raise ValueError("agent spec must be 'path:function' (e.g. agent.py:answer)")
    target, func = spec.split(":", 1)
    p = Path(target)
    if p.suffix == ".py":
        # Ensure the agent's directory is on sys.path so relative imports work.
        agent_dir = str(p.resolve().parent)
        if agent_dir not in sys.path:
            sys.path.insert(0, agent_dir)
        mod_spec = importlib.util.spec_from_file_location(p.stem, p)
        if mod_spec is None or mod_spec.loader is None:
            raise ImportError(f"could not load {target}")
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_spec.name] = module
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    if not hasattr(module, func):
        raise AttributeError(f"{target} has no attribute {func!r}")
    return getattr(module, func)


def _make_agent(callable_: Callable[..., Any]) -> Callable[[EvalCase], Any]:
    async def _agent(case: EvalCase) -> Trajectory:
        result = callable_(case.input)
        if asyncio.iscoroutine(result):
            answer_text = await result
        else:
            answer_text = result
        return Trajectory(
            request_id=case.id,
            final_answer=str(answer_text),
            retrieved_contexts=case.expected_contexts,
            extra={"input_text": case.input},
        )
    return _agent


def render_report(report: EvalReport, fmt: str) -> str:
    if fmt == "json":
        return emit_json(report)
    if fmt == "html":
        return emit_html(report)
    if fmt == "junit":
        return emit_junit(report)
    raise ValueError(f"unknown report format {fmt!r}")


async def run_eval(
    *,
    dataset: Path,
    agent_spec: str,
    threshold: float,
    report_fmt: str,
    output: Path | None,
) -> tuple[EvalReport, str]:
    cases = EvalRunner.load_dataset(dataset)
    callable_ = _load_callable(agent_spec)
    runner = EvalRunner(
        agent=_make_agent(callable_),
        scorers=[FaithfulnessScorer(judge=DeterministicJudge())],
        threshold=threshold,
    )
    report = await runner.run(cases)
    rendered = render_report(report, report_fmt)
    if output is not None:
        output.write_text(rendered)
    return report, rendered
