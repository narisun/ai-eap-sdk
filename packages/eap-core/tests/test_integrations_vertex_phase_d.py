"""Tests for Vertex Phase D: Registry, Payments (AP2), Evaluations."""

from __future__ import annotations

import pytest

from eap_core.discovery import AgentRegistry
from eap_core.exceptions import RealRuntimeDisabledError
from eap_core.integrations.vertex import (
    AP2PaymentClient,
    VertexAgentRegistry,
    VertexEvalScorer,
    to_vertex_eval_dataset,
)
from eap_core.payments import PaymentBackend, PaymentRequired


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


# ---- VertexAgentRegistry --------------------------------------------------


def test_registry_construction_does_not_hit_google_cloud():
    import sys

    sys.modules.pop("google.cloud.aiplatform_v1beta1", None)
    _ = VertexAgentRegistry(project_id="p", registry_id="r")
    assert "google.cloud.aiplatform_v1beta1" not in sys.modules


def test_registry_satisfies_agent_registry_protocol():
    """``VertexAgentRegistry`` must structurally conform to ``AgentRegistry``."""
    r = VertexAgentRegistry(project_id="p")
    assert isinstance(r, AgentRegistry)
    assert r.name == "vertex_agent_registry"


def test_registry_parent_path_format():
    r = VertexAgentRegistry(project_id="p1", location="europe-west1", registry_id="my-reg")
    assert r._parent() == "projects/p1/locations/europe-west1/agentRegistries/my-reg"


@pytest.mark.asyncio
async def test_registry_publish_requires_name():
    """The Protocol contract: publish must require a 'name' field
    (validated *before* the env-flag gate so config bugs surface
    even when REAL_RUNTIMES is unset)."""
    r = VertexAgentRegistry(project_id="p")
    with pytest.raises(ValueError, match="name"):
        await r.publish({"description": "no name"})


@pytest.mark.asyncio
async def test_registry_publish_gated_by_env_flag():
    r = VertexAgentRegistry(project_id="p")
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await r.publish({"name": "agent-a"})


@pytest.mark.asyncio
async def test_registry_get_gated():
    r = VertexAgentRegistry(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await r.get("agent-a")


@pytest.mark.asyncio
async def test_registry_search_gated():
    r = VertexAgentRegistry(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await r.search("query")


@pytest.mark.asyncio
async def test_registry_list_records_gated():
    r = VertexAgentRegistry(project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await r.list_records()


# ---- AP2PaymentClient ------------------------------------------------------


def test_ap2_construction_does_not_hit_google_cloud():
    import sys

    sys.modules.pop("google.cloud.aiplatform_v1beta1", None)
    _ = AP2PaymentClient(wallet_provider_id="w1", project_id="p")
    assert "google.cloud.aiplatform_v1beta1" not in sys.modules


def test_ap2_satisfies_payment_backend_protocol():
    c = AP2PaymentClient(wallet_provider_id="w1", project_id="p")
    assert isinstance(c, PaymentBackend)
    assert c.name == "ap2_payment"


def test_ap2_initial_budget_state():
    c = AP2PaymentClient(wallet_provider_id="w1", project_id="p", max_spend_cents=500)
    assert c.spent_cents == 0
    assert c.remaining_cents == 500


def test_ap2_can_afford_within_budget():
    c = AP2PaymentClient(wallet_provider_id="w1", project_id="p", max_spend_cents=100)
    assert c.can_afford(50) is True
    assert c.can_afford(100) is True
    assert c.can_afford(101) is False


def test_ap2_session_id_starts_none():
    c = AP2PaymentClient(wallet_provider_id="w1", project_id="p")
    assert c.session_id is None


@pytest.mark.asyncio
async def test_ap2_start_session_gated():
    c = AP2PaymentClient(wallet_provider_id="w1", project_id="p")
    with pytest.raises(RealRuntimeDisabledError):
        await c.start_session()


@pytest.mark.asyncio
async def test_ap2_authorize_gated():
    c = AP2PaymentClient(wallet_provider_id="w1", project_id="p")
    req = PaymentRequired(
        amount_cents=10,
        currency="USD",
        merchant="m",
        original_url="https://api.example.com/x",
    )
    with pytest.raises(RealRuntimeDisabledError):
        await c.authorize(req)


# ---- Eval adapters --------------------------------------------------------


def test_to_vertex_eval_dataset_maps_fields():
    """``to_vertex_eval_dataset`` should map Trajectory fields to
    Vertex-Eval shape (prompt/response/context/trace_id/steps)."""
    from eap_core.eval.trajectory import Trajectory

    t = Trajectory(
        request_id="req-1",
        final_answer="Paris",
        retrieved_contexts=["fr.wikipedia.org/Paris"],
        steps=[],
        extra={"input_text": "Capital of France?"},
    )
    rows = to_vertex_eval_dataset([t])
    assert len(rows) == 1
    r = rows[0]
    assert r["trace_id"] == "req-1"
    assert r["prompt"] == "Capital of France?"
    assert r["response"] == "Paris"
    assert r["context"] == ["fr.wikipedia.org/Paris"]
    assert r["steps"] == []


def test_to_vertex_eval_dataset_handles_missing_input_text():
    from eap_core.eval.trajectory import Trajectory

    t = Trajectory(
        request_id="req-2",
        final_answer="answer",
        retrieved_contexts=[],
        steps=[],
        extra={},
    )
    rows = to_vertex_eval_dataset([t])
    assert rows[0]["prompt"] == ""


def test_eval_scorer_construction():
    s = VertexEvalScorer(project_id="p", metric="faithfulness")
    assert s.name == "vertex_eval"


def test_eval_scorer_custom_name():
    s = VertexEvalScorer(project_id="p", metric="groundedness", scorer_name="my_grounder")
    assert s.name == "my_grounder"


@pytest.mark.asyncio
async def test_eval_scorer_score_gated():
    from eap_core.eval.trajectory import Trajectory

    s = VertexEvalScorer(project_id="p", metric="faithfulness")
    t = Trajectory(
        request_id="req-1",
        final_answer="ans",
        retrieved_contexts=[],
        steps=[],
        extra={"input_text": "q"},
    )
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await s.score(t)
