import pytest

pytest.importorskip("ragas")
pytestmark = pytest.mark.extras

from eap_core.eval.ragas_adapter import to_ragas_dataset
from eap_core.eval.trajectory import Trajectory


def test_converts_trajectories_to_ragas_dataset_dicts():
    trajs = [
        Trajectory(
            request_id="r1",
            final_answer="Paris is the capital.",
            retrieved_contexts=["Paris is the capital of France."],
            extra={"input_text": "What is the capital of France?"},
        ),
        Trajectory(
            request_id="r2",
            final_answer="Lyon is in France.",
            retrieved_contexts=["Lyon is the third-largest city in France."],
            extra={"input_text": "Where is Lyon?"},
        ),
    ]
    rows = to_ragas_dataset(trajs)
    assert len(rows) == 2
    assert rows[0]["question"] == "What is the capital of France?"
    assert rows[0]["answer"] == "Paris is the capital."
    assert rows[0]["contexts"] == ["Paris is the capital of France."]
