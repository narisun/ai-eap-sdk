"""Tests for Vertex Phase C: Gateway client."""

from __future__ import annotations

import pytest

from eap_core.exceptions import RealRuntimeDisabledError
from eap_core.integrations.vertex import VertexGatewayClient


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


def test_gateway_client_construction_does_not_open_connection():
    """Building the client must not perform network I/O."""
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    assert c._url == "https://gw.example.com/mcp"


def test_gateway_url_is_normalized():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp/")
    assert c._url == "https://gw.example.com/mcp"


def test_audience_defaults_to_url():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    assert c._audience == "https://gw.example.com/mcp"


def test_explicit_audience_overrides_url():
    c = VertexGatewayClient(
        gateway_url="https://gw.example.com/mcp",
        audience="https://api.example.com",
    )
    assert c._audience == "https://api.example.com"


@pytest.mark.asyncio
async def test_list_tools_gated_by_env_flag():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await c.list_tools()


@pytest.mark.asyncio
async def test_invoke_gated_by_env_flag():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    with pytest.raises(RealRuntimeDisabledError):
        await c.invoke("foo", {"x": 1})


@pytest.mark.asyncio
async def test_bearer_header_empty_without_identity():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    assert await c._bearer_header() == {}


@pytest.mark.asyncio
async def test_bearer_header_uses_identity():
    # ``VertexAgentIdentityToken`` keeps a SYNC ``get_token`` (wraps
    # google.auth, which is sync). The gateway's ``_bearer_header`` is
    # async and detects sync-vs-async tokens via ``asyncio.iscoroutine``
    # — so a sync identity like this one still flows through cleanly.
    class FakeIdentity:
        def get_token(self, *, audience: str | None = None, scope: str = "") -> str:
            return f"token-for-{audience}-{scope}"

    c = VertexGatewayClient(
        gateway_url="https://gw.example.com/mcp",
        identity=FakeIdentity(),
        scope="ai.write",
    )
    h = await c._bearer_header()
    assert h == {"Authorization": "Bearer token-for-https://gw.example.com/mcp-ai.write"}


@pytest.mark.asyncio
async def test_bearer_header_supports_async_identity():
    """H2: ``NonHumanIdentity.get_token`` is async. ``_bearer_header``
    must await it; the awaitable-aware dispatch in ``_bearer_header``
    handles both sync (Vertex) and async (NHI) identities."""

    class AsyncIdentity:
        async def get_token(self, *, audience: str | None = None, scope: str = "") -> str:
            return f"async-token-{audience}-{scope}"

    c = VertexGatewayClient(
        gateway_url="https://gw.example.com/mcp",
        identity=AsyncIdentity(),
        scope="read",
    )
    h = await c._bearer_header()
    assert h == {"Authorization": "Bearer async-token-https://gw.example.com/mcp-read"}


def test_request_id_increments_per_call():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    assert c._next_id() == 1
    assert c._next_id() == 2
    assert c._next_id() == 3


@pytest.mark.asyncio
async def test_rpc_with_stub_http_returns_result(monkeypatch):
    """Smoke-test the JSON-RPC envelope handling end-to-end with a stub
    httpx client. The env-flag gate sits in `list_tools`/`invoke`;
    `_rpc` itself is unconditional, so we exercise it directly."""

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "t"}]}}

        text = ""

    captured: dict = {}

    class FakeAsyncClient:
        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse()

        async def aclose(self) -> None:
            pass

    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp", http=FakeAsyncClient())
    result = await c._rpc("tools/list", {})
    assert result == {"tools": [{"name": "t"}]}
    payload = captured["kwargs"]["json"]
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "tools/list"
    assert payload["params"] == {}


@pytest.mark.asyncio
async def test_rpc_propagates_error_body(monkeypatch):
    from eap_core.mcp.types import MCPError

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "method not found"},
            }

        text = ""

    class FakeAsyncClient:
        async def post(self, url, **kwargs):
            return FakeResponse()

        async def aclose(self) -> None:
            pass

    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp", http=FakeAsyncClient())
    with pytest.raises(MCPError, match="method not found"):
        await c._rpc("tools/list", {})


@pytest.mark.asyncio
async def test_rpc_propagates_http_status(monkeypatch):
    from eap_core.mcp.types import MCPError

    class FakeResponse:
        status_code = 503
        text = "service unavailable"

        @staticmethod
        def json() -> dict:
            return {}

    class FakeAsyncClient:
        async def post(self, url, **kwargs):
            return FakeResponse()

        async def aclose(self) -> None:
            pass

    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp", http=FakeAsyncClient())
    with pytest.raises(MCPError, match="HTTP 503"):
        await c._rpc("tools/list", {})


@pytest.mark.asyncio
async def test_aclose_closes_owned_http_only():
    """``aclose`` closes the pool only when ``VertexGatewayClient`` created it.

    A caller-supplied ``http=`` is treated as borrowed — closing it on the
    caller's behalf would break their app if they still use that pool
    elsewhere. See Task 8 (H1) httpx-ownership tracking.
    """
    closed = {"v": False}

    class FakeAsyncClient:
        async def post(self, url, **kwargs):
            raise NotImplementedError

        async def aclose(self) -> None:
            closed["v"] = True

    # Caller-supplied: borrowed; aclose must NOT close.
    borrowed = FakeAsyncClient()
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp", http=borrowed)
    await c.aclose()
    assert closed["v"] is False, "borrowed http client must not be closed by aclose"

    # Self-constructed: owned; aclose closes.
    c2 = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    c2._http = FakeAsyncClient()  # type: ignore[assignment]
    c2._owns_http = True
    await c2.aclose()
    assert closed["v"] is True
