"""Tests for Phase C AgentCore Gateway integration."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from eap_core.integrations.agentcore import (
    GatewayClient,
    add_gateway_to_registry,
    export_tools_as_openapi,
)
from eap_core.mcp import McpToolRegistry, mcp_tool


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


# ---- GatewayClient -------------------------------------------------------


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


def _make_client(handler) -> GatewayClient:
    """Build a GatewayClient backed by a custom mock transport."""
    http = httpx.AsyncClient(transport=_MockTransport(handler))
    return GatewayClient(gateway_url="https://gw.example", http=http)


async def test_gateway_list_tools_raises_without_env_flag():
    gw = GatewayClient(gateway_url="https://gw.example")
    with pytest.raises(NotImplementedError):
        await gw.list_tools()


async def test_gateway_invoke_raises_without_env_flag():
    gw = GatewayClient(gateway_url="https://gw.example")
    with pytest.raises(NotImplementedError):
        await gw.invoke("foo", {})


async def test_gateway_construction_does_not_hit_network():
    """Building a GatewayClient must not make any HTTP calls."""
    calls = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    _make_client(_handler)
    assert calls["n"] == 0


async def test_gateway_list_tools_sends_jsonrpc_tools_list(monkeypatch):
    """With the env flag set, list_tools sends a JSON-RPC tools/list request."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")
    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.content.decode()
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {"name": "ping", "description": "ping a host", "inputSchema": {}},
                    ]
                },
            },
        )

    gw = _make_client(_handler)
    tools = await gw.list_tools()
    assert tools == [{"name": "ping", "description": "ping a host", "inputSchema": {}}]
    import json

    body = json.loads(captured["body"])
    assert body["method"] == "tools/list"
    assert body["jsonrpc"] == "2.0"
    assert captured["url"] == "https://gw.example"


async def test_gateway_invoke_returns_text_when_single_text_content(monkeypatch):
    """The MCP tools/call response wraps results in `content`; we surface
    the text directly when there's exactly one TextContent."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "pong"}]},
            },
        )

    gw = _make_client(_handler)
    result = await gw.invoke("ping", {"host": "example.com"})
    assert result == "pong"


async def test_gateway_invoke_returns_full_content_when_multipart(monkeypatch):
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ]
                },
            },
        )

    gw = _make_client(_handler)
    result = await gw.invoke("multi", {})
    assert result == [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]


async def test_gateway_raises_on_jsonrpc_error(monkeypatch):
    from eap_core.mcp.types import MCPError

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32601, "message": "Method not found"},
            },
        )

    gw = _make_client(_handler)
    with pytest.raises(MCPError, match="Method not found"):
        await gw.invoke("missing-tool", {})


async def test_gateway_raises_on_http_error(monkeypatch):
    from eap_core.mcp.types import MCPError

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    gw = _make_client(_handler)
    with pytest.raises(MCPError, match="HTTP 503"):
        await gw.invoke("anything", {})


async def test_gateway_attaches_bearer_token_from_identity(monkeypatch):
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")
    captured: dict[str, Any] = {}

    def _handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    class _FakeIdentity:
        def get_token(self, *, audience: str, scope: str) -> str:
            return "fake-token-123"

    http = httpx.AsyncClient(transport=_MockTransport(_handler))
    gw = GatewayClient(
        gateway_url="https://gw.example",
        identity=_FakeIdentity(),
        audience="my-gateway",
        http=http,
    )
    await gw.invoke("any", {})
    assert captured["headers"]["authorization"] == "Bearer fake-token-123"


# ---- add_gateway_to_registry --------------------------------------------


async def test_add_gateway_to_registry_registers_proxy_specs():
    """Remote tool specs from the gateway get registered as proxy ToolSpecs
    whose fn forwards to gateway.invoke."""
    reg = McpToolRegistry()

    class _StubGateway:
        def __init__(self):
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def invoke(self, name: str, args: dict[str, Any]) -> Any:
            self.calls.append((name, args))
            return f"called {name} with {args}"

    gw = _StubGateway()
    specs = [
        {"name": "lookup_account", "description": "Look up an account.", "inputSchema": {}},
        {"name": "get_balance", "description": "Get a balance.", "inputSchema": {}},
    ]
    count = add_gateway_to_registry(reg, gw, specs)
    assert count == 2
    assert {s.name for s in reg.list_tools()} == {"lookup_account", "get_balance"}

    # Dispatching through the registry forwards to the gateway. Gateway
    # proxy specs are marked ``requires_auth=True`` (see the dedicated
    # test below) so the dispatcher requires an ``identity`` — pass a
    # stub one to exercise the forwarding path.
    result = await reg.invoke("lookup_account", {"id": "acct-1"}, identity=object())
    assert result == "called lookup_account with {'id': 'acct-1'}"
    assert gw.calls == [("lookup_account", {"id": "acct-1"})]


def test_add_gateway_to_registry_skips_unnamed_specs():
    reg = McpToolRegistry()

    class _StubGateway:
        async def invoke(self, name: str, args: dict[str, Any]) -> Any:
            return None

    specs = [
        {"name": "good", "description": "ok", "inputSchema": {}},
        {"description": "no name", "inputSchema": {}},  # missing name
    ]
    count = add_gateway_to_registry(reg, _StubGateway(), specs)
    assert count == 1


def test_add_gateway_to_registry_marks_proxies_as_auth_required():
    """Remote tool calls cross a network boundary; mark requires_auth=True."""
    reg = McpToolRegistry()

    class _StubGateway:
        async def invoke(self, name: str, args: dict[str, Any]) -> Any:
            return None

    add_gateway_to_registry(
        reg, _StubGateway(), [{"name": "x", "description": "", "inputSchema": {}}]
    )
    spec = reg.get("x")
    assert spec is not None
    assert spec.requires_auth is True


def test_add_gateway_to_registry_handles_input_schema_aliases():
    """Some MCP-server flavors return camelCase, others snake_case."""
    reg = McpToolRegistry()

    class _StubGateway:
        async def invoke(self, name: str, args: dict[str, Any]) -> Any:
            return None

    schema_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema_b = {"type": "object", "properties": {"b": {"type": "integer"}}}
    add_gateway_to_registry(
        reg,
        _StubGateway(),
        [
            {"name": "via_camel", "description": "", "inputSchema": schema_a},
            {"name": "via_snake", "description": "", "input_schema": schema_b},
        ],
    )
    assert reg.get("via_camel").input_schema == schema_a  # type: ignore[union-attr]
    assert reg.get("via_snake").input_schema == schema_b  # type: ignore[union-attr]


# ---- export_tools_as_openapi -------------------------------------------


def test_export_tools_as_openapi_emits_one_path_per_tool():
    reg = McpToolRegistry()

    @mcp_tool(description="Add two numbers.")
    async def add_(a: int, b: int) -> int:
        return a + b

    @mcp_tool(description="Echo input.")
    async def echo_(s: str) -> str:
        return s

    reg.register(add_.spec)
    reg.register(echo_.spec)

    spec = export_tools_as_openapi(reg, title="My Tools", version="1.2.3")
    assert spec["openapi"] == "3.1.0"
    assert spec["info"] == {"title": "My Tools", "version": "1.2.3"}
    assert "/tools/add_" in spec["paths"]
    assert "/tools/echo_" in spec["paths"]

    add_op = spec["paths"]["/tools/add_"]["post"]
    assert add_op["operationId"] == "add_"
    assert "requestBody" in add_op
    body_schema = add_op["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["type"] == "object"
    assert "a" in body_schema["properties"]
    assert "b" in body_schema["properties"]


def test_export_tools_as_openapi_marks_auth_required_in_extension():
    reg = McpToolRegistry()

    @mcp_tool(description="Sensitive op.", requires_auth=True)
    async def sensitive_(x: int) -> int:
        return x

    reg.register(sensitive_.spec)
    spec = export_tools_as_openapi(reg)
    op = spec["paths"]["/tools/sensitive_"]["post"]
    assert op["x-mcp-tool"]["requires_auth"] is True


def test_export_tools_as_openapi_empty_registry_produces_valid_skeleton():
    reg = McpToolRegistry()
    spec = export_tools_as_openapi(reg)
    assert spec["paths"] == {}
    assert spec["openapi"] == "3.1.0"


# ---- publish-to-gateway CLI -------------------------------------------


def test_publish_to_gateway_writes_openapi_and_readme(tmp_path, monkeypatch):
    """End-to-end: a project with an @mcp_tool produces openapi.json + README."""
    from click.testing import CliRunner
    from eap_cli.main import cli

    project = tmp_path / "demo"
    project.mkdir()
    (project / "agent.py").write_text(
        "from eap_core.mcp import McpToolRegistry, mcp_tool\n"
        "\n"
        "@mcp_tool(description='Look up an account.')\n"
        "async def lookup_account(account_id: str) -> dict:\n"
        "    return {'id': account_id}\n"
        "\n"
        "registry = McpToolRegistry()\n"
        "registry.register(lookup_account.spec)\n"
    )
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(cli, ["publish-to-gateway", "--title", "demo-tools"])
    assert result.exit_code == 0, result.output

    out = project / "dist" / "gateway"
    assert (out / "openapi.json").is_file()
    assert (out / "README.md").is_file()

    import json

    spec = json.loads((out / "openapi.json").read_text())
    assert "/tools/lookup_account" in spec["paths"]
    assert spec["info"]["title"] == "demo-tools"


def test_publish_to_gateway_dry_run_writes_nothing(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from eap_cli.main import cli

    project = tmp_path / "demo"
    project.mkdir()
    (project / "agent.py").write_text("# empty\n")
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(cli, ["publish-to-gateway", "--dry-run"])
    assert result.exit_code == 0
    assert not (project / "dist").exists()


def test_publish_to_gateway_missing_entry_errors_clearly(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from eap_cli.main import cli

    project = tmp_path / "demo"
    project.mkdir()
    monkeypatch.chdir(project)

    runner = CliRunner()
    result = runner.invoke(cli, ["publish-to-gateway", "--entry", "missing.py"])
    assert result.exit_code != 0
