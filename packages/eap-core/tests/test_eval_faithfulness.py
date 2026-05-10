import pytest

from eap_core.eval.faithfulness import (
    DeterministicJudge,
    FaithfulnessResult,
    FaithfulnessScorer,
    Verdict,
)
from eap_core.eval.trajectory import Trajectory


async def test_deterministic_judge_extracts_sentences_as_claims():
    judge = DeterministicJudge()
    claims = await judge.extract_claims("Paris is the capital. The Eiffel Tower is iconic.")
    assert len(claims) == 2
    assert "Paris" in claims[0]


@pytest.mark.parametrize(
    "claim, contexts, expected",
    [
        ("Paris is the capital of France", ["Paris is the capital of France."], Verdict.SUPPORTED),
        ("The moon is made of cheese", ["The moon is a rocky body."], Verdict.NOT_FOUND),
        ("Paris is the capital", ["Lyon is the capital of France"], Verdict.NOT_FOUND),
    ],
)
async def test_deterministic_judge_entailment(claim, contexts, expected):
    judge = DeterministicJudge()
    verdict = await judge.entails(claim, contexts)
    assert verdict == expected


async def test_faithfulness_score_full_support():
    judge = DeterministicJudge()
    scorer = FaithfulnessScorer(judge=judge)
    traj = Trajectory(
        request_id="r1",
        final_answer="Paris is the capital of France. The Eiffel Tower is in Paris.",
        retrieved_contexts=[
            "Paris is the capital of France.",
            "The Eiffel Tower is in Paris and is a famous landmark.",
        ],
    )
    result = await scorer.score(traj)
    assert isinstance(result, FaithfulnessResult)
    assert result.score == 1.0
    assert len(result.per_claim) == 2
    assert all(item.verdict == Verdict.SUPPORTED for item in result.per_claim)


async def test_faithfulness_score_partial():
    judge = DeterministicJudge()
    scorer = FaithfulnessScorer(judge=judge)
    traj = Trajectory(
        request_id="r2",
        final_answer="Paris is the capital. Mars has two moons.",
        retrieved_contexts=["Paris is the capital of France."],
    )
    result = await scorer.score(traj)
    assert 0.0 < result.score < 1.0


async def test_faithfulness_score_zero_when_empty_answer():
    judge = DeterministicJudge()
    scorer = FaithfulnessScorer(judge=judge)
    traj = Trajectory(request_id="r3", final_answer="", retrieved_contexts=["x"])
    result = await scorer.score(traj)
    assert result.score == 0.0
    assert result.per_claim == []
