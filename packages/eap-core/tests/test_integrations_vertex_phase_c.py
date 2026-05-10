"""Tests for Vertex Phase C: Gateway client."""

from __future__ import annotations

import pytest

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
    with pytest.raises(NotImplementedError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await c.list_tools()


@pytest.mark.asyncio
async def test_invoke_gated_by_env_flag():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    with pytest.raises(NotImplementedError):
        await c.invoke("foo", {"x": 1})


def test_bearer_header_empty_without_identity():
    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp")
    assert c._bearer_header() == {}


def test_bearer_header_uses_identity():
    class FakeIdentity:
        def get_token(self, *, audience: str | None = None, scope: str = "") -> str:
            return f"token-for-{audience}-{scope}"

    c = VertexGatewayClient(
        gateway_url="https://gw.example.com/mcp",
        identity=FakeIdentity(),
        scope="ai.write",
    )
    h = c._bearer_header()
    assert h == {"Authorization": "Bearer token-for-https://gw.example.com/mcp-ai.write"}


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
async def test_aclose_closes_underlying_http():
    closed = {"v": False}

    class FakeAsyncClient:
        async def post(self, url, **kwargs):
            raise NotImplementedError

        async def aclose(self) -> None:
            closed["v"] = True

    c = VertexGatewayClient(gateway_url="https://gw.example.com/mcp", http=FakeAsyncClient())
    await c.aclose()
    assert closed["v"] is True
