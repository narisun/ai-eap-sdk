"""Mocked real-runtime tests for ``integrations/vertex.py``.

These tests exercise the ``EAP_ENABLE_REAL_RUNTIMES=1`` code paths in
the Vertex integration by injecting a fake ``google.cloud.aiplatform_v1beta1``
module into ``sys.modules``. The fake module exposes the five service-client
constructors the integration uses (MemoryBank, Sandbox, AgentRegistry,
Payment, Evaluation); each returns a ``MagicMock`` whose method returns the
test can pre-configure.

Live GCP calls (H19) remain deferred.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from eap_core.integrations.vertex import (
    AP2PaymentClient,
    VertexAgentRegistry,
    VertexBrowserSandbox,
    VertexCodeSandbox,
    VertexEvalScorer,
    VertexMemoryBankStore,
)

# ---- VertexMemoryBankStore -----------------------------------------------


def test_memory_bank_client_uses_memory_bank_service_client(
    mock_google_aiplatform: dict[str, MagicMock],
) -> None:
    """The store's ``_client()`` must construct a MemoryBankServiceClient,
    NOT one of the other four available client types (SandboxServiceClient,
    AgentRegistryServiceClient, etc.)."""
    store = VertexMemoryBankStore(project_id="proj-1", memory_bank_id="mb-1")
    client = store._client()
    assert client is mock_google_aiplatform["MemoryBankServiceClient"]
    mock_google_aiplatform["MemoryBankServiceClient"]._ctor.assert_called_once_with()
    # No other constructor was invoked.
    assert mock_google_aiplatform["SandboxServiceClient"]._ctor.call_count == 0


async def test_memory_bank_remember_calls_upsert_memory_with_parent(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``remember`` must call ``upsert_memory`` with parent / session_id /
    key / value forwarded verbatim. The parent path encodes project,
    location, and memory-bank id."""
    store = VertexMemoryBankStore(
        project_id="proj-1", location="us-central1", memory_bank_id="mb-A"
    )
    await store.remember("sess-1", "name", "alice")
    client = mock_google_aiplatform["MemoryBankServiceClient"]
    client.upsert_memory.assert_called_once_with(
        parent="projects/proj-1/locations/us-central1/memoryBanks/mb-A",
        session_id="sess-1",
        key="name",
        value="alice",
    )


async def test_memory_bank_list_keys_projects_key_attr(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``list_keys`` reads ``.key`` from each memory record on the response."""
    client = mock_google_aiplatform["MemoryBankServiceClient"]

    class _Mem:
        def __init__(self, key: str) -> None:
            self.key = key

    resp = MagicMock()
    resp.memories = [_Mem("name"), _Mem("email")]
    client.list_memories.return_value = resp

    store = VertexMemoryBankStore(project_id="proj-1", memory_bank_id="mb-A")
    keys = await store.list_keys("sess-1")
    assert keys == ["name", "email"]
    client.list_memories.assert_called_once_with(
        parent="projects/proj-1/locations/us-central1/memoryBanks/mb-A",
        session_id="sess-1",
    )


async def test_memory_bank_forget_and_clear_call_distinct_methods(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``forget`` deletes a single record; ``clear`` deletes the whole session.
    The two methods must NOT collapse to the same API call."""
    store = VertexMemoryBankStore(project_id="proj-1", memory_bank_id="mb-A")
    await store.forget("sess-1", "name")
    await store.clear("sess-1")
    client = mock_google_aiplatform["MemoryBankServiceClient"]
    client.delete_memory.assert_called_once_with(
        parent="projects/proj-1/locations/us-central1/memoryBanks/mb-A",
        session_id="sess-1",
        key="name",
    )
    client.delete_session.assert_called_once_with(
        parent="projects/proj-1/locations/us-central1/memoryBanks/mb-A",
        session_id="sess-1",
    )


# ---- VertexCodeSandbox ---------------------------------------------------


def test_code_sandbox_client_uses_sandbox_service_client(
    mock_google_aiplatform: dict[str, MagicMock],
) -> None:
    sandbox = VertexCodeSandbox(project_id="proj-1")
    client = sandbox._client()
    assert client is mock_google_aiplatform["SandboxServiceClient"]
    mock_google_aiplatform["SandboxServiceClient"]._ctor.assert_called_once_with()


async def test_code_sandbox_execute_maps_response_to_sandbox_result(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``execute`` calls ``execute_code(parent=, language=, code=, sandbox_id=)``
    and maps the response onto ``SandboxResult`` (stdout / stderr / exit_code /
    artifacts dict)."""
    from eap_core.sandbox import SandboxResult

    client = mock_google_aiplatform["SandboxServiceClient"]

    class _Artifact:
        def __init__(self, name: str, uri: str) -> None:
            self.name = name
            self.uri = uri

    resp = MagicMock()
    resp.stdout = "hello\n"
    resp.stderr = ""
    resp.exit_code = 0
    resp.artifacts = [_Artifact("out.txt", "gs://bkt/out.txt")]
    client.execute_code.return_value = resp

    sandbox = VertexCodeSandbox(project_id="proj-X", location="europe-west1", sandbox_id="sb-1")
    result = await sandbox.execute("python", "print('hi')")
    assert isinstance(result, SandboxResult)
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.artifacts == {"out.txt": "gs://bkt/out.txt"}

    client.execute_code.assert_called_once_with(
        parent="projects/proj-X/locations/europe-west1",
        language="python",
        code="print('hi')",
        sandbox_id="sb-1",
    )


async def test_code_sandbox_register_tools_js_ts_forward_distinct_languages(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``register_code_sandbox_tools`` adds three wrappers — each must
    forward its own language string. A swap (e.g. JS calling Python) would
    surface here."""
    from eap_core.integrations.vertex import register_code_sandbox_tools
    from eap_core.mcp.registry import McpToolRegistry

    client = mock_google_aiplatform["SandboxServiceClient"]
    resp = MagicMock()
    resp.stdout = "ok"
    resp.stderr = ""
    resp.exit_code = 0
    resp.artifacts = []
    client.execute_code.return_value = resp

    reg = McpToolRegistry()
    register_code_sandbox_tools(reg, project_id="proj-1")
    js_tool = reg.get("execute_javascript")
    ts_tool = reg.get("execute_typescript")
    assert js_tool is not None and ts_tool is not None

    out_js = await js_tool.fn(code="x=1")
    out_ts = await ts_tool.fn(code="const x: number = 1")
    assert out_js == {"stdout": "ok", "stderr": "", "exit_code": 0}
    assert out_ts == {"stdout": "ok", "stderr": "", "exit_code": 0}
    calls = client.execute_code.call_args_list
    assert [c.kwargs["language"] for c in calls] == ["javascript", "typescript"]


# ---- VertexBrowserSandbox ------------------------------------------------


def test_browser_sandbox_client_uses_sandbox_service_client(
    mock_google_aiplatform: dict[str, MagicMock],
) -> None:
    browser = VertexBrowserSandbox(project_id="proj-1", session_id="b-1")
    client = browser._client()
    assert client is mock_google_aiplatform["SandboxServiceClient"]


async def test_browser_sandbox_action_forwards_action_and_params(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``navigate`` / ``click`` / ``fill`` / ``extract_text`` / ``screenshot``
    all funnel through ``_action`` with distinct action strings.

    Distinguishing each call's ``action=`` argument catches a future
    refactor that drops the parameter or swaps it."""
    client = mock_google_aiplatform["SandboxServiceClient"]

    resp = MagicMock()
    resp.result = {"text": "Hello world"}
    client.invoke_browser_action.return_value = resp

    browser = VertexBrowserSandbox(project_id="proj-X", session_id="bs-1")
    text = await browser.extract_text(selector="h1")
    assert text == "Hello world"
    call_kwargs = client.invoke_browser_action.call_args.kwargs
    assert call_kwargs["action"] == "extract_text"
    assert call_kwargs["parent"] == "projects/proj-X/locations/us-central1"
    assert call_kwargs["session_id"] == "bs-1"
    assert call_kwargs["params"] == {"selector": "h1"}


async def test_browser_sandbox_screenshot_decodes_base64_to_bytes(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``screenshot`` reads ``png_base64`` from the response and decodes
    it to raw bytes for the Protocol."""
    import base64

    client = mock_google_aiplatform["SandboxServiceClient"]
    payload = b"\x89PNG\r\n\x1a\nfoo"
    encoded = base64.b64encode(payload).decode()
    resp = MagicMock()
    resp.result = {"png_base64": encoded}
    client.invoke_browser_action.return_value = resp

    browser = VertexBrowserSandbox(project_id="proj-1")
    out = await browser.screenshot()
    assert out == payload


async def test_browser_sandbox_action_returns_empty_dict_when_result_falsy(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """When the API response has ``result=None``, ``_action`` returns ``{}``
    rather than raising — keeps callers off TypeError when an action
    succeeds but returns no payload."""
    client = mock_google_aiplatform["SandboxServiceClient"]
    resp = MagicMock()
    resp.result = None
    client.invoke_browser_action.return_value = resp

    browser = VertexBrowserSandbox(project_id="proj-1")
    out = await browser.navigate("https://example.com")
    assert out == {}


async def test_browser_sandbox_register_tools_click_fill_navigate_all_forward(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """The five browser-sandbox tool wrappers each call the corresponding
    browser method. Sanity-checks that the registration helper wires the
    right actions on each spec."""
    from eap_core.integrations.vertex import register_browser_sandbox_tools
    from eap_core.mcp.registry import McpToolRegistry

    client = mock_google_aiplatform["SandboxServiceClient"]
    resp = MagicMock()
    resp.result = {"text": "Title here"}
    client.invoke_browser_action.return_value = resp

    reg = McpToolRegistry()
    register_browser_sandbox_tools(reg, project_id="proj-1", session_id="s-1")

    await reg.get("browser_navigate").fn(url="https://example.com")  # type: ignore[union-attr]
    await reg.get("browser_click").fn(selector="#go")  # type: ignore[union-attr]
    await reg.get("browser_fill").fn(selector="#q", value="hello")  # type: ignore[union-attr]
    text_out = await reg.get("browser_extract_text").fn(selector="#t")  # type: ignore[union-attr]
    shot_out = await reg.get("browser_screenshot").fn()  # type: ignore[union-attr]

    actions = [c.kwargs["action"] for c in client.invoke_browser_action.call_args_list]
    assert actions == ["navigate", "click", "fill", "extract_text", "screenshot"]
    # extract_text projects ``text``; screenshot encodes back to base64.
    assert text_out == "Title here"
    assert "png_base64" in shot_out


# ---- VertexAgentRegistry -------------------------------------------------


def test_vertex_agent_registry_client_uses_agent_registry_service_client(
    mock_google_aiplatform: dict[str, MagicMock],
) -> None:
    r = VertexAgentRegistry(project_id="proj-1")
    client = r._client()
    assert client is mock_google_aiplatform["AgentRegistryServiceClient"]


async def test_vertex_agent_registry_publish_forwards_name_and_returns_record_id(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``publish`` calls ``create_registry_record`` with parent (project+location+
    registry_id), record_type, name, description, metadata, and returns the
    stringified ``record_id``."""
    client = mock_google_aiplatform["AgentRegistryServiceClient"]
    resp = MagicMock()
    resp.record_id = "rec-1"
    client.create_registry_record.return_value = resp

    r = VertexAgentRegistry(project_id="proj-1", location="us-central1", registry_id="default")
    rid = await r.publish(
        {
            "name": "billing-agent",
            "description": "handles billing",
            "record_type": "AGENT",
            "version": "1",
        }
    )
    assert rid == "rec-1"
    call_kwargs = client.create_registry_record.call_args.kwargs
    assert call_kwargs["parent"] == (
        "projects/proj-1/locations/us-central1/agentRegistries/default"
    )
    assert call_kwargs["name"] == "billing-agent"
    assert call_kwargs["description"] == "handles billing"
    assert call_kwargs["record_type"] == "AGENT"


async def test_vertex_agent_registry_publish_rejects_record_without_name() -> None:
    """``publish`` validates the record HAS a name BEFORE consulting the
    runtime gate — so users get a fast, clear error even with the env flag
    disabled."""
    r = VertexAgentRegistry(project_id="proj-1")
    with pytest.raises(ValueError, match="'name' field"):
        await r.publish({"description": "no name"})


async def test_vertex_agent_registry_search_forwards_query(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``search`` forwards query + max_results and projects the records list."""
    client = mock_google_aiplatform["AgentRegistryServiceClient"]
    rec1 = {"name": "a", "recordType": "AGENT"}
    rec2 = {"name": "b", "recordType": "MCP_SERVER"}
    resp = MagicMock()
    resp.records = [rec1, rec2]
    client.search_registry_records.return_value = resp

    r = VertexAgentRegistry(project_id="proj-1", registry_id="default")
    out = await r.search("invoice", max_results=20)
    assert out == [rec1, rec2]
    call_kwargs = client.search_registry_records.call_args.kwargs
    assert call_kwargs["query"] == "invoice"
    assert call_kwargs["max_results"] == 20
    assert call_kwargs["parent"].endswith("/agentRegistries/default")


async def test_vertex_agent_registry_list_records_omits_type_when_none(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """When ``record_type`` is None, ``list_records`` must NOT pass it as
    a kwarg — otherwise an explicit ``None`` would shadow the default
    ListRecords filter behavior."""
    client = mock_google_aiplatform["AgentRegistryServiceClient"]
    resp = MagicMock()
    resp.records = []
    client.list_registry_records.return_value = resp

    r = VertexAgentRegistry(project_id="proj-1", registry_id="default")
    out = await r.list_records()
    assert out == []
    call_kwargs = client.list_registry_records.call_args.kwargs
    assert "record_type" not in call_kwargs
    assert call_kwargs["max_results"] == 100


async def test_vertex_agent_registry_get_returns_none_on_any_exception(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``get`` uses a broad ``except Exception`` (mirrors AgentCore's
    contract) — any client error surfaces as 'absent'. This pins that
    contract so a future narrowing surfaces here."""
    client = mock_google_aiplatform["AgentRegistryServiceClient"]
    client.get_registry_record.side_effect = RuntimeError("transport blew up")

    r = VertexAgentRegistry(project_id="proj-1")
    out = await r.get("unknown-agent")
    assert out is None


# ---- AP2PaymentClient ----------------------------------------------------


def test_ap2_payment_client_uses_payment_service_client(
    mock_google_aiplatform: dict[str, MagicMock],
) -> None:
    pc = AP2PaymentClient(wallet_provider_id="w", project_id="proj-1", max_spend_cents=100)
    pc._client()
    mock_google_aiplatform["PaymentServiceClient"]._ctor.assert_called_once_with()


async def test_ap2_payment_start_session_caches_session_id(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``start_session`` forwards budget config and caches the returned
    session_id on the client. The parent path uses project + location."""
    client = mock_google_aiplatform["PaymentServiceClient"]
    resp = MagicMock()
    resp.session_id = "ap2-sess-1"
    client.create_payment_session.return_value = resp

    pc = AP2PaymentClient(
        wallet_provider_id="w-1",
        project_id="proj-1",
        max_spend_cents=500,
        location="us-central1",
        currency="USD",
        session_ttl_seconds=60,
    )
    sid = await pc.start_session()
    assert sid == "ap2-sess-1"
    assert pc.session_id == "ap2-sess-1"
    call_kwargs = client.create_payment_session.call_args.kwargs
    assert call_kwargs["parent"] == "projects/proj-1/locations/us-central1"
    assert call_kwargs["wallet_provider_id"] == "w-1"
    assert call_kwargs["max_spend_amount_cents"] == 500
    assert call_kwargs["currency"] == "USD"
    assert call_kwargs["ttl_seconds"] == 60


async def test_ap2_payment_authorize_deducts_budget_and_returns_receipt(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """Happy-path: start a session, authorize a charge under budget,
    receipt comes back and ``spent_cents`` updates."""
    client = mock_google_aiplatform["PaymentServiceClient"]
    sess_resp = MagicMock()
    sess_resp.session_id = "ap2-7"
    client.create_payment_session.return_value = sess_resp

    auth_resp = MagicMock()
    auth_resp.receipt = {"signature": "sig-z", "txn": "txn-9"}
    client.authorize_payment.return_value = auth_resp

    pc = AP2PaymentClient(wallet_provider_id="w", project_id="proj-1", max_spend_cents=1000)
    await pc.start_session()

    class _Req:
        amount_cents = 200
        currency = "USD"
        merchant = "acme"
        original_url = "https://acme.example/api"

    receipt = await pc.authorize(_Req())
    assert receipt == {"signature": "sig-z", "txn": "txn-9"}
    assert pc.spent_cents == 200
    assert pc.remaining_cents == 800
    call_kwargs = client.authorize_payment.call_args.kwargs
    assert call_kwargs["session_id"] == "ap2-7"
    assert call_kwargs["amount_cents"] == 200
    assert call_kwargs["currency"] == "USD"
    assert call_kwargs["merchant"] == "acme"
    assert call_kwargs["original_url"] == "https://acme.example/api"


# ---- VertexEvalScorer ----------------------------------------------------


def test_vertex_eval_scorer_client_uses_evaluation_service_client(
    mock_google_aiplatform: dict[str, MagicMock],
) -> None:
    scorer = VertexEvalScorer(project_id="proj-1", metric="faithfulness")
    scorer._client()
    mock_google_aiplatform["EvaluationServiceClient"]._ctor.assert_called_once_with()


async def test_vertex_eval_scorer_score_maps_trajectory_and_surfaces_result(
    mock_google_aiplatform: dict[str, MagicMock],
    real_runtimes_enabled: None,
) -> None:
    """``score`` maps trajectory.prompt/response/contexts onto
    evaluate_instance(instance=...), and surfaces score + explanation
    into FaithfulnessResult."""
    from eap_core.eval.faithfulness import FaithfulnessResult

    client = mock_google_aiplatform["EvaluationServiceClient"]
    resp = MagicMock()
    resp.score = 0.91
    resp.explanation = "Faithful to context."
    client.evaluate_instance.return_value = resp

    class _Step:
        def model_dump(self) -> dict[str, Any]:
            return {"role": "user"}

    class _Traj:
        def __init__(self) -> None:
            self.request_id = "req-7"
            self.final_answer = "Paris."
            self.retrieved_contexts: list[str] = ["France's capital is Paris."]
            self.steps: list[Any] = [_Step()]
            self.extra: dict[str, Any] = {"input_text": "What is the capital of France?"}

    scorer = VertexEvalScorer(project_id="proj-1", location="us-central1", metric="faithfulness")
    result = await scorer.score(_Traj())
    assert isinstance(result, FaithfulnessResult)
    assert result.request_id == "req-7"
    assert result.score == 0.91
    assert result.notes == "Faithful to context."

    call_kwargs = client.evaluate_instance.call_args.kwargs
    assert call_kwargs["parent"] == "projects/proj-1/locations/us-central1"
    assert call_kwargs["metric"] == "faithfulness"
    assert call_kwargs["instance"]["prompt"] == "What is the capital of France?"
    assert call_kwargs["instance"]["response"] == "Paris."
    assert call_kwargs["instance"]["context"] == ["France's capital is Paris."]


# ---- VertexGatewayClient lifecycle --------------------------------------


async def test_vertex_gateway_client_aclose_closes_owned_http_pool() -> None:
    """``async with`` allocates and closes the verifier-owned ``AsyncClient``."""
    from eap_core.integrations.vertex import VertexGatewayClient

    async with VertexGatewayClient(gateway_url="https://gw.example") as gw:
        assert gw._owns_http is True
        owned = gw._http
        assert owned.is_closed is False
    assert owned.is_closed is True


async def test_vertex_gateway_client_forwards_custom_auth_to_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``auth=`` is set, the VertexGatewayClient passes it through to
    ``http.post(...)``. Mirrors the AgentCore sibling test — same contract
    on the symmetric class."""
    import httpx

    from eap_core.integrations.vertex import VertexGatewayClient

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    captured: dict[str, Any] = {}

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    class _RecordingAuth(httpx.Auth):
        def auth_flow(self, request: httpx.Request):
            captured["seen"] = True
            yield request

    http = httpx.AsyncClient(transport=_MockTransport())
    gw = VertexGatewayClient(gateway_url="https://gw.example", http=http, auth=_RecordingAuth())
    await gw.invoke("noop", {})
    assert captured.get("seen") is True
