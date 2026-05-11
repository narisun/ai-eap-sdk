"""Mocked real-runtime tests for ``integrations/agentcore.py``.

These tests exercise the ``EAP_ENABLE_REAL_RUNTIMES=1`` code paths in
the AgentCore integration by injecting a fake ``boto3`` module (the
real one isn't installed in the non-extras venv). Each test asserts
both that the integration called ``boto3`` with the expected shape AND
that the integration's return value matches the shape the caller will
see — so a future regression that drops a parameter or returns the
wrong field surfaces immediately.

Live AWS calls (H18) remain deferred.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from eap_core.integrations.agentcore import (
    AgentCoreEvalScorer,
    AgentCoreMemoryStore,
    PaymentClient,
    PaymentRequired,
    RegistryClient,
)

# ---- AgentCoreMemoryStore._client + remember/recall/list/forget/clear ----


def test_memory_store_client_uses_correct_service_and_region(
    mock_boto3_client: MagicMock,
) -> None:
    """``_client()`` must call ``boto3.client('bedrock-agentcore', region_name=<r>)``."""
    store = AgentCoreMemoryStore(memory_id="mem-x", region="eu-west-1")
    returned = store._client()
    # Same MagicMock the integration will use for the API call.
    assert returned is mock_boto3_client
    mock_boto3_client._factory.assert_called_once_with("bedrock-agentcore", region_name="eu-west-1")


async def test_memory_remember_invokes_put_memory_record_with_expected_args(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``remember`` calls ``put_memory_record`` with memoryId / sessionId /
    recordKey / recordValue."""
    store = AgentCoreMemoryStore(memory_id="mem-1", region="us-east-1")
    await store.remember("sess-A", "name", "alice")
    mock_boto3_client.put_memory_record.assert_called_once_with(
        memoryId="mem-1",
        sessionId="sess-A",
        recordKey="name",
        recordValue="alice",
    )


async def test_memory_recall_returns_string_value_from_response(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``recall`` reads ``recordValue`` from the response and stringifies it."""
    mock_boto3_client.get_memory_record.return_value = {"recordValue": "alice"}
    store = AgentCoreMemoryStore(memory_id="mem-1")
    value = await store.recall("sess-A", "name")
    assert value == "alice"
    mock_boto3_client.get_memory_record.assert_called_once_with(
        memoryId="mem-1", sessionId="sess-A", recordKey="name"
    )


async def test_memory_recall_returns_none_when_response_has_no_value(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """If the boto response omits ``recordValue``, recall returns None."""
    mock_boto3_client.get_memory_record.return_value = {}
    store = AgentCoreMemoryStore(memory_id="mem-1")
    assert await store.recall("sess-A", "absent") is None


async def test_memory_list_keys_extracts_record_keys_from_response(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``list_keys`` projects ``records[*].recordKey`` from the API response."""
    mock_boto3_client.list_memory_records.return_value = {
        "records": [{"recordKey": "name"}, {"recordKey": "email"}],
    }
    store = AgentCoreMemoryStore(memory_id="mem-1")
    keys = await store.list_keys("sess-A")
    assert keys == ["name", "email"]
    mock_boto3_client.list_memory_records.assert_called_once_with(
        memoryId="mem-1", sessionId="sess-A"
    )


async def test_memory_forget_calls_delete_memory_record(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    store = AgentCoreMemoryStore(memory_id="mem-1")
    await store.forget("sess-A", "name")
    mock_boto3_client.delete_memory_record.assert_called_once_with(
        memoryId="mem-1", sessionId="sess-A", recordKey="name"
    )


async def test_memory_clear_calls_delete_memory_session(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    store = AgentCoreMemoryStore(memory_id="mem-1")
    await store.clear("sess-A")
    mock_boto3_client.delete_memory_session.assert_called_once_with(
        memoryId="mem-1", sessionId="sess-A"
    )


# ---- RegistryClient ------------------------------------------------------


def test_registry_client_uses_control_plane_service(
    mock_boto3_client: MagicMock,
) -> None:
    """RegistryClient uses ``bedrock-agentcore-control`` (control plane),
    not the data-plane ``bedrock-agentcore`` service."""
    rc = RegistryClient(registry_name="my-reg", region="us-west-2")
    rc._client()
    mock_boto3_client._factory.assert_called_once_with(
        "bedrock-agentcore-control", region_name="us-west-2"
    )


async def test_registry_publish_mcp_server_passes_endpoint_metadata(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``publish_mcp_server`` packs ``mcpEndpoint`` into metadata and returns
    the recordId surfaced by the API."""
    mock_boto3_client.create_registry_record.return_value = {"recordId": "rec-42"}
    rc = RegistryClient(registry_name="my-reg")
    rid = await rc.publish_mcp_server(
        "my-server",
        description="my MCP server",
        mcp_endpoint="https://mcp.example/sse",
    )
    assert rid == "rec-42"
    mock_boto3_client.create_registry_record.assert_called_once_with(
        registryName="my-reg",
        recordType="MCP_SERVER",
        name="my-server",
        description="my MCP server",
        metadata={"mcpEndpoint": "https://mcp.example/sse"},
    )


async def test_registry_publish_agent_card_serializes_via_model_dump(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``publish_agent_card`` calls ``card.model_dump()`` and forwards
    ``name`` + ``description`` from the dumped body."""
    mock_boto3_client.create_registry_record.return_value = {"recordId": "rec-7"}

    class _FakeCard:
        def __init__(self) -> None:
            self.dumped = False

        def model_dump(self) -> dict[str, Any]:
            self.dumped = True
            return {
                "name": "billing-agent",
                "description": "handles billing",
                "version": "1.0",
            }

    card = _FakeCard()
    rc = RegistryClient(registry_name="my-reg")
    rid = await rc.publish_agent_card(card)
    assert card.dumped is True
    assert rid == "rec-7"
    call = mock_boto3_client.create_registry_record.call_args
    assert call.kwargs["registryName"] == "my-reg"
    assert call.kwargs["recordType"] == "AGENT"
    assert call.kwargs["name"] == "billing-agent"
    assert call.kwargs["description"] == "handles billing"
    # The full dumped body is passed as metadata — including version.
    assert call.kwargs["metadata"]["version"] == "1.0"


async def test_registry_search_forwards_query_and_returns_records(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``search`` forwards query + maxResults and returns the records list."""
    mock_boto3_client.search_registry_records.return_value = {
        "records": [{"name": "a"}, {"name": "b"}],
    }
    rc = RegistryClient(registry_name="my-reg")
    out = await rc.search("invoice", max_results=5)
    assert out == [{"name": "a"}, {"name": "b"}]
    mock_boto3_client.search_registry_records.assert_called_once_with(
        registryName="my-reg", query="invoice", maxResults=5
    )


async def test_registry_list_records_omits_type_when_not_provided(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """When ``record_type`` is ``None``, list_records must NOT pass a
    ``recordType`` kwarg — passing ``None`` would be a different filter."""
    mock_boto3_client.list_registry_records.return_value = {"records": []}
    rc = RegistryClient(registry_name="my-reg")
    await rc.list_records(max_results=50)
    call_kwargs = mock_boto3_client.list_registry_records.call_args.kwargs
    assert "recordType" not in call_kwargs
    assert call_kwargs == {"registryName": "my-reg", "maxResults": 50}


async def test_registry_list_records_includes_type_filter_when_provided(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    mock_boto3_client.list_registry_records.return_value = {
        "records": [{"name": "x", "recordType": "MCP_SERVER"}],
    }
    rc = RegistryClient(registry_name="my-reg")
    out = await rc.list_records(record_type="MCP_SERVER", max_results=10)
    assert out == [{"name": "x", "recordType": "MCP_SERVER"}]
    mock_boto3_client.list_registry_records.assert_called_once_with(
        registryName="my-reg", maxResults=10, recordType="MCP_SERVER"
    )


async def test_registry_get_record_returns_record_dict_on_hit(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``get_record`` unwraps the ``record`` field of the boto3 response."""
    mock_boto3_client.get_registry_record.return_value = {
        "record": {"name": "billing-agent", "recordType": "AGENT"},
    }
    rc = RegistryClient(registry_name="my-reg")
    rec = await rc.get_record("billing-agent")
    assert rec == {"name": "billing-agent", "recordType": "AGENT"}
    mock_boto3_client.get_registry_record.assert_called_once_with(
        registryName="my-reg", name="billing-agent"
    )


async def test_registry_get_record_returns_none_on_resource_not_found(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """When boto3 raises ``ResourceNotFoundException``, get_record returns None."""

    class _RnfError(Exception):
        """Stand-in for boto3's dynamically-generated ResourceNotFoundException."""

    # boto3 dynamically generates exception classes on the client.
    mock_boto3_client.exceptions.ResourceNotFoundException = _RnfError
    mock_boto3_client.get_registry_record.side_effect = _RnfError("absent")
    rc = RegistryClient(registry_name="my-reg")
    assert await rc.get_record("absent-agent") is None


# ---- PaymentClient ------------------------------------------------------


def test_payment_client_uses_data_plane_service(
    mock_boto3_client: MagicMock,
) -> None:
    """PaymentClient uses ``bedrock-agentcore`` (data plane) in the
    configured region. Distinct from RegistryClient's control-plane use."""
    pc = PaymentClient(wallet_provider_id="wallet-1", max_spend_cents=500, region="ap-south-1")
    pc._client()
    mock_boto3_client._factory.assert_called_once_with(
        "bedrock-agentcore", region_name="ap-south-1"
    )


async def test_payment_start_session_passes_budget_and_caches_session_id(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``start_session`` forwards budget + ttl + currency and caches
    the returned sessionId on the client."""
    mock_boto3_client.create_payment_session.return_value = {"sessionId": "ps-1"}
    pc = PaymentClient(
        wallet_provider_id="wallet-1",
        max_spend_cents=200,
        currency="EUR",
        session_ttl_seconds=120,
    )
    sid = await pc.start_session()
    assert sid == "ps-1"
    assert pc.session_id == "ps-1"
    mock_boto3_client.create_payment_session.assert_called_once_with(
        walletProviderId="wallet-1",
        maxSpendAmountCents=200,
        currency="EUR",
        ttlSeconds=120,
    )


async def test_payment_authorize_and_retry_signs_and_deducts_budget(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """Happy-path: start a session, authorize a charge under budget,
    receive the signed receipt, and spent_cents updates."""
    mock_boto3_client.create_payment_session.return_value = {"sessionId": "ps-9"}
    mock_boto3_client.authorize_payment.return_value = {
        "receipt": {"signature": "sig-abc", "txn": "txn-1"},
    }
    pc = PaymentClient(wallet_provider_id="w", max_spend_cents=1000)
    await pc.start_session()

    req = PaymentRequired(
        amount_cents=250,
        currency="USD",
        merchant="acme",
        original_url="https://acme.example/api",
    )
    receipt = await pc.authorize_and_retry(req)
    assert receipt == {"signature": "sig-abc", "txn": "txn-1"}
    assert pc.spent_cents == 250
    assert pc.remaining_cents == 750
    mock_boto3_client.authorize_payment.assert_called_once_with(
        sessionId="ps-9",
        amountCents=250,
        currency="USD",
        merchant="acme",
        originalUrl="https://acme.example/api",
    )


# ---- AgentCoreEvalScorer ------------------------------------------------


def test_agentcore_eval_scorer_client_uses_data_plane_service(
    mock_boto3_client: MagicMock,
) -> None:
    scorer = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness",
        region="us-east-1",
    )
    scorer._client()
    mock_boto3_client._factory.assert_called_once_with("bedrock-agentcore", region_name="us-east-1")


async def test_agentcore_eval_scorer_score_calls_evaluate_trace_with_mapped_fields(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """``score`` maps trajectory.question/answer/contexts onto evaluate_trace
    input and surfaces the API's score + explanation into FaithfulnessResult."""
    from eap_core.eval.faithfulness import FaithfulnessResult

    mock_boto3_client.evaluate_trace.return_value = {
        "score": 0.875,
        "explanation": "Answer is grounded.",
    }

    class _Step:
        def model_dump(self) -> dict[str, Any]:
            return {"role": "user"}

    class _Traj:
        def __init__(self) -> None:
            self.request_id = "req-42"
            self.final_answer = "Paris."
            self.retrieved_contexts: list[str] = ["France's capital is Paris."]
            self.steps: list[Any] = [_Step()]
            self.extra: dict[str, Any] = {"input_text": "What is the capital of France?"}

    scorer = AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness",
        region="us-east-1",
    )
    result = await scorer.score(_Traj())
    assert isinstance(result, FaithfulnessResult)
    assert result.request_id == "req-42"
    assert result.score == 0.875
    assert result.notes == "Answer is grounded."

    call_kwargs = mock_boto3_client.evaluate_trace.call_args.kwargs
    assert (
        call_kwargs["evaluatorArn"] == "arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness"
    )
    assert call_kwargs["input"]["question"] == "What is the capital of France?"
    assert call_kwargs["input"]["answer"] == "Paris."
    assert call_kwargs["input"]["contexts"] == ["France's capital is Paris."]


# ---- Code Interpreter + Browser tool registration ----------------------
#
# The Phase B register_*_tools helpers create multiple closures that share
# the same _execute / _browser_call body. Existing tests only invoke ONE
# of each (and only at the gate-disabled path). These tests exercise the
# remaining language / action closures to lock down that each is wired
# correctly and forwards its language/action argument verbatim.


async def test_code_interpreter_typescript_and_javascript_call_through_boto3(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """The JS and TS tool closures must forward their distinct language
    string to ``invoke_code_interpreter`` — a swap would surface here."""
    from eap_core.integrations.agentcore import register_code_interpreter_tools
    from eap_core.mcp.registry import McpToolRegistry

    mock_boto3_client.invoke_code_interpreter.return_value = {
        "stdout": "ok",
        "stderr": "",
        "exitCode": 0,
    }
    reg = McpToolRegistry()
    register_code_interpreter_tools(reg, region="us-east-1", session_id="sess-CI")
    js_tool = reg.get("execute_javascript")
    ts_tool = reg.get("execute_typescript")
    assert js_tool is not None and ts_tool is not None

    js_out = await js_tool.fn(code="console.log(1)")
    ts_out = await ts_tool.fn(code="const x: number = 1")
    assert js_out == {"stdout": "ok", "stderr": "", "exit_code": 0}
    assert ts_out == {"stdout": "ok", "stderr": "", "exit_code": 0}

    # Each invocation must reach boto3 with a DIFFERENT language string.
    calls = mock_boto3_client.invoke_code_interpreter.call_args_list
    assert len(calls) == 2
    languages = [c.kwargs["language"] for c in calls]
    assert languages == ["javascript", "typescript"]
    # sessionId forwards from the helper's keyword.
    assert all(c.kwargs["sessionId"] == "sess-CI" for c in calls)


async def test_browser_click_fill_extract_screenshot_forward_distinct_actions(
    mock_boto3_client: MagicMock,
    real_runtimes_enabled: None,
) -> None:
    """The five browser closures share one ``_browser_call`` body; each
    forwards its own action string and kwargs. A swap (e.g. click sending
    'fill') would surface here."""
    from eap_core.integrations.agentcore import register_browser_tools
    from eap_core.mcp.registry import McpToolRegistry

    # Set up responses keyed by what the test will call.
    mock_boto3_client.invoke_browser_action.side_effect = [
        {"ok": True},  # click
        {"ok": True},  # fill
        {"text": "Hello"},  # extract_text
        {"png_base64": "Zm9v"},  # screenshot
    ]
    reg = McpToolRegistry()
    register_browser_tools(reg, region="us-east-1", session_id="sess-B")

    click_out = await reg.get("browser_click").fn(selector="#go")  # type: ignore[union-attr]
    fill_out = await reg.get("browser_fill").fn(selector="#q", value="hello")  # type: ignore[union-attr]
    text_out = await reg.get("browser_extract_text").fn(selector="#title")  # type: ignore[union-attr]
    shot_out = await reg.get("browser_screenshot").fn()  # type: ignore[union-attr]

    # click + fill surface the raw dict; extract_text projects ``text``;
    # screenshot returns the dict unchanged.
    assert click_out == {"ok": True}
    assert fill_out == {"ok": True}
    assert text_out == "Hello"
    assert shot_out == {"png_base64": "Zm9v"}

    calls = mock_boto3_client.invoke_browser_action.call_args_list
    actions = [c.kwargs["action"] for c in calls]
    assert actions == ["click", "fill", "extract_text", "screenshot"]
    # The action-specific kwargs must reach boto3 as named arguments.
    assert calls[0].kwargs["selector"] == "#go"
    assert calls[1].kwargs["selector"] == "#q"
    assert calls[1].kwargs["value"] == "hello"
    assert calls[2].kwargs["selector"] == "#title"
    # sessionId forwards on every call from the helper's keyword.
    assert all(c.kwargs["sessionId"] == "sess-B" for c in calls)


# ---- GatewayClient lifecycle (aclose, __aenter__/__aexit__) ------------


async def test_gateway_client_aclose_closes_owned_http_pool() -> None:
    """When the caller does NOT supply ``http``, the GatewayClient owns
    its pool and ``aclose()`` closes it. ``__aenter__`` / ``__aexit__``
    use the same path."""
    from eap_core.integrations.agentcore import GatewayClient

    async with GatewayClient(gateway_url="https://gw.example") as gw:
        assert gw._owns_http is True
        owned = gw._http
        assert owned.is_closed is False
    # After __aexit__, aclose was called.
    assert owned.is_closed is True


async def test_gateway_client_aclose_does_not_close_caller_supplied_http() -> None:
    """Caller-supplied ``http`` is never closed by the client — ownership
    stays with the caller."""
    import httpx

    from eap_core.integrations.agentcore import GatewayClient

    caller_http = httpx.AsyncClient()
    gw = GatewayClient(gateway_url="https://gw.example", http=caller_http)
    assert gw._owns_http is False
    await gw.aclose()
    assert caller_http.is_closed is False
    await caller_http.aclose()


async def test_gateway_client_forwards_custom_auth_to_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``auth=`` is set, the GatewayClient must pass it through to
    the underlying ``http.post(...)`` so SigV4 / arbitrary httpx auth
    work end-to-end."""
    import httpx

    from eap_core.integrations.agentcore import GatewayClient

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    captured: dict[str, Any] = {}

    class _MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}},
            )

    class _RecordingAuth(httpx.Auth):
        def auth_flow(self, request: httpx.Request):
            captured["seen"] = True
            request.headers["X-Sentinel"] = "yes"
            yield request

    auth = _RecordingAuth()
    http = httpx.AsyncClient(transport=_MockTransport())
    gw = GatewayClient(gateway_url="https://gw.example", http=http, auth=auth)
    # Drive an invoke so _rpc runs the auth-attaching branch.
    await gw.invoke("noop", {})
    # Auth was reached, meaning post_kwargs["auth"] was set.
    assert captured.get("seen") is True
