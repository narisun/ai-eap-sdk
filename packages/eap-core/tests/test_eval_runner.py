import json

from eap_core.eval.faithfulness import DeterministicJudge, FaithfulnessScorer
from eap_core.eval.runner import EvalCase, EvalReport, EvalRunner
from eap_core.eval.trajectory import Trajectory


async def _agent_returns_full_support(case: EvalCase) -> Trajectory:
    return Trajectory(
        request_id=case.id,
        final_answer=" ".join(case.expected_contexts),
        retrieved_contexts=case.expected_contexts,
    )


async def _agent_returns_unsupported(case: EvalCase) -> Trajectory:
    return Trajectory(
        request_id=case.id,
        final_answer="Mars has unicorns.",
        retrieved_contexts=case.expected_contexts,
    )


async def test_runner_scores_each_case_and_aggregates():
    cases = [
        EvalCase(id="c1", input="q1", expected_contexts=["Paris is the capital of France."]),
        EvalCase(id="c2", input="q2", expected_contexts=["The Eiffel Tower is in Paris."]),
    ]
    runner = EvalRunner(
        agent=_agent_returns_full_support,
        scorers=[FaithfulnessScorer(judge=DeterministicJudge())],
    )
    report = await runner.run(cases)
    assert isinstance(report, EvalReport)
    assert len(report.cases) == 2
    assert report.cases[0].scores["faithfulness"].score == 1.0
    assert report.aggregate["faithfulness"] == 1.0


async def test_runner_marks_failures_below_threshold():
    cases = [EvalCase(id="c1", input="q", expected_contexts=["Paris is in France."])]
    runner = EvalRunner(
        agent=_agent_returns_unsupported,
        scorers=[FaithfulnessScorer(judge=DeterministicJudge())],
        threshold=0.7,
    )
    report = await runner.run(cases)
    assert report.failed_count == 1
    assert report.passed_count == 0
    assert report.aggregate["faithfulness"] < 0.7


async def test_runner_loads_dataset_from_json(tmp_path):
    dataset = tmp_path / "golden.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "id": "c1",
                    "input": "q1",
                    "expected_contexts": ["X"],
                    "expected_answer_substrings": ["X"],
                },
            ]
        )
    )
    cases = EvalRunner.load_dataset(dataset)
    assert len(cases) == 1
    assert cases[0].id == "c1"
    assert cases[0].expected_answer_substrings == ["X"]


async def test_eval_case_minimal_fields():
    c = EvalCase(id="x", input="hi", expected_contexts=[])
    assert c.expected_answer_substrings == []
