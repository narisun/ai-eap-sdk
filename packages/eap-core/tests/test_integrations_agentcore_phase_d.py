"""Tests for Phase D AgentCore integrations: Registry, Payments, Evaluations."""

from __future__ import annotations

import pytest

from eap_core.a2a import AgentCard, Skill
from eap_core.eval import Trajectory
from eap_core.eval.trajectory import Step
from eap_core.integrations.agentcore import (
    AgentCoreEvalScorer,
    PaymentClient,
    PaymentRequired,
    RegistryClient,
    to_agentcore_eval_dataset,
)


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


# ---- RegistryClient -----------------------------------------------------


def test_registry_client_construction_is_cheap():
    """Building a RegistryClient does no I/O."""
    import sys

    sys.modules.pop("boto3", None)
    _ = RegistryClient(registry_name="org-registry", region="us-east-1")
    assert "boto3" not in sys.modules


async def test_registry_publish_agent_card_gated_by_env_flag():
    rc = RegistryClient(registry_name="org-registry")
    card = AgentCard(
        name="bank-agent",
        description="banking ops",
        skills=[Skill(name="get_balance", description="...", input_schema={})],
    )
    with pytest.raises(NotImplementedError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await rc.publish_agent_card(card)


async def test_registry_publish_mcp_server_gated():
    rc = RegistryClient(registry_name="org-registry")
    with pytest.raises(NotImplementedError):
        await rc.publish_mcp_server(
            "my-mcp", description="...", mcp_endpoint="https://mcp.example/mcp"
        )


async def test_registry_get_record_gated():
    rc = RegistryClient(registry_name="org-registry")
    with pytest.raises(NotImplementedError):
        await rc.get_record("some-name")


async def test_registry_search_gated():
    rc = RegistryClient(registry_name="org-registry")
    with pytest.raises(NotImplementedError):
        await rc.search("find banking agents")


async def test_registry_list_records_gated():
    rc = RegistryClient(registry_name="org-registry")
    with pytest.raises(NotImplementedError):
        await rc.list_records()
    with pytest.raises(NotImplementedError):
        await rc.list_records(record_type="AGENT", max_results=50)


def test_registry_client_stores_construction_params():
    rc = RegistryClient(registry_name="org-reg", region="eu-west-1")
    assert rc._registry_name == "org-reg"
    assert rc._region == "eu-west-1"


# ---- PaymentRequired exception ------------------------------------------


def test_payment_required_carries_x402_metadata():
    pr = PaymentRequired(
        amount_cents=50,
        currency="USD",
        merchant="paid-api.example",
        original_url="https://paid-api.example/v1/data",
        raw={"protocol": "x402"},
    )
    assert pr.amount_cents == 50
    assert pr.currency == "USD"
    assert pr.merchant == "paid-api.example"
    assert pr.original_url == "https://paid-api.example/v1/data"
    assert pr.raw == {"protocol": "x402"}
    assert "payment required" in str(pr).lower()
    assert "50 USD to paid-api.example" in str(pr)


def test_payment_required_optional_raw_defaults_empty():
    pr = PaymentRequired(
        amount_cents=10,
        currency="USD",
        merchant="x",
        original_url="https://x",
    )
    assert pr.raw == {}


# ---- PaymentClient ------------------------------------------------------


def test_payment_client_initial_state():
    pc = PaymentClient(
        wallet_provider_id="cdp-wallet-1",
        max_spend_cents=200,
        session_ttl_seconds=600,
    )
    assert pc.session_id is None
    assert pc.spent_cents == 0
    assert pc.remaining_cents == 200


async def test_payment_client_start_session_gated():
    pc = PaymentClient(wallet_provider_id="cdp-wallet-1", max_spend_cents=100)
    with pytest.raises(NotImplementedError):
        await pc.start_session()


async def test_payment_client_authorize_gated():
    pc = PaymentClient(wallet_provider_id="cdp-wallet-1", max_spend_cents=100)
    pr = PaymentRequired(amount_cents=50, currency="USD", merchant="x", original_url="https://x")
    with pytest.raises(NotImplementedError):
        await pc.authorize_and_retry(pr)


def test_payment_client_can_afford_respects_budget():
    pc = PaymentClient(wallet_provider_id="w", max_spend_cents=100)
    assert pc.can_afford(50) is True
    assert pc.can_afford(100) is True
    assert pc.can_afford(101) is False


def test_payment_client_remaining_tracks_spend():
    """Budget bookkeeping should be deterministic from the client's own state."""
    pc = PaymentClient(wallet_provider_id="w", max_spend_cents=100)
    assert pc.remaining_cents == 100
    # Simulate a successful authorize by bumping spent (live path is gated;
    # bookkeeping is just integer math).
    pc._spent_cents = 30
    assert pc.remaining_cents == 70
    assert pc.can_afford(70) is True
    assert pc.can_afford(71) is False


def test_payment_client_construction_is_cheap():
    """No boto3 import at construction time."""
    import sys

    sys.modules.pop("boto3", None)
    _ = PaymentClient(wallet_provider_id="w", max_spend_cents=100)
    assert "boto3" not in sys.modules


# ---- Evaluation adapters ------------------------------------------------


def _sample_trajectory(req_id: str = "r1", with_input: bool = True) -> Trajectory:
    extra = {"input_text": "What is the capital of France?"} if with_input else {}
    return Trajectory(
        request_id=req_id,
        steps=[Step(role="assistant", text="Paris.", input_tokens=5, output_tokens=2)],
        final_answer="Paris.",
        retrieved_contexts=["Paris is the capital of France."],
        extra=extra,
    )


def test_to_agentcore_eval_dataset_emits_one_row_per_trajectory():
    trajs = [_sample_trajectory("r1"), _sample_trajectory("r2")]
    rows = to_agentcore_eval_dataset(trajs)
    assert len(rows) == 2
    assert rows[0]["trace_id"] == "r1"
    assert rows[0]["answer"] == "Paris."
    assert rows[0]["question"] == "What is the capital of France?"
    assert rows[0]["contexts"] == ["Paris is the capital of France."]


def test_to_agentcore_eval_dataset_handles_missing_input_text():
    """When extra doesn't have input_text, the question field is empty."""
    rows = to_agentcore_eval_dataset([_sample_trajectory(with_input=False)])
    assert rows[0]["question"] == ""


def test_to_agentcore_eval_dataset_serializes_steps():
    rows = to_agentcore_eval_dataset([_sample_trajectory()])
    assert "steps" in rows[0]
    assert isinstance(rows[0]["steps"], list)
    assert rows[0]["steps"][0]["text"] == "Paris."
    assert rows[0]["steps"][0]["role"] == "assistant"


def test_to_agentcore_eval_dataset_empty_list_returns_empty():
    assert to_agentcore_eval_dataset([]) == []


# ---- AgentCoreEvalScorer ------------------------------------------------


def test_agentcore_eval_scorer_default_name():
    s = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness"
    )
    assert s.name == "agentcore_eval"


def test_agentcore_eval_scorer_custom_name():
    s = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness",
        scorer_name="helpfulness_v2",
    )
    assert s.name == "helpfulness_v2"


async def test_agentcore_eval_scorer_gated_by_env_flag():
    s = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness"
    )
    traj = _sample_trajectory()
    with pytest.raises(NotImplementedError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await s.score(traj)


def test_agentcore_eval_scorer_construction_is_cheap():
    import sys

    sys.modules.pop("boto3", None)
    _ = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness"
    )
    assert "boto3" not in sys.modules


# ---- Integration smoke: scorer protocol fit -----------------------------


def test_agentcore_scorer_satisfies_runner_scorer_shape():
    """The scorer must have ``name: str`` and ``async score(traj)``."""
    s = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness"
    )
    # Duck-typing: structural compatibility check.
    assert hasattr(s, "name")
    assert callable(getattr(s, "score", None))
    assert isinstance(s.name, str)
