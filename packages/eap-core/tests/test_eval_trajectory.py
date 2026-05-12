import json

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.eval.trajectory import Step, Trajectory, TrajectoryRecorder
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.policy import PolicyMiddleware, SimpleJsonPolicyEvaluator

PERMIT_ALL = {
    "version": "1",
    "rules": [
        {"id": "permit", "effect": "permit", "principal": "*", "action": "*", "resource": "*"}
    ],
}


def test_trajectory_step_carries_role_and_text():
    step = Step(role="assistant", text="hi", input_tokens=1, output_tokens=2)
    assert step.role == "assistant"
    assert step.input_tokens == 1


def test_trajectory_serializes_to_jsonable_dict():
    traj = Trajectory(
        request_id="r1",
        steps=[Step(role="assistant", text="ok", input_tokens=3, output_tokens=1)],
        final_answer="ok",
        retrieved_contexts=["c1"],
    )
    d = traj.model_dump()
    assert d["request_id"] == "r1"
    assert d["steps"][0]["text"] == "ok"


async def test_recorder_writes_jsonl_per_request(tmp_path):
    out = tmp_path / "traces.jsonl"
    recorder = TrajectoryRecorder(out_path=out)
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            ObservabilityMiddleware(),
            PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL)),
            recorder,
        ],
    )
    await client.generate_text("hello world")
    await client.generate_text("another prompt")

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["request_id"]
    assert rec["final_answer"]


async def test_recorder_collects_retrieved_contexts_from_ctx(tmp_path):
    out = tmp_path / "traces.jsonl"
    recorder = TrajectoryRecorder(out_path=out)

    from eap_core.middleware.base import PassthroughMiddleware
    from eap_core.types import Context, Request

    class CtxStuffer(PassthroughMiddleware):
        name = "stuffer"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            ctx.metadata["retrieved_contexts"] = ["doc:1 says X", "doc:2 says Y"]
            return req

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            CtxStuffer(),
            ObservabilityMiddleware(),
            PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL)),
            recorder,
        ],
    )
    await client.generate_text("hello")
    line = out.read_text().strip().splitlines()[0]
    rec = json.loads(line)
    assert rec["retrieved_contexts"] == ["doc:1 says X", "doc:2 says Y"]
