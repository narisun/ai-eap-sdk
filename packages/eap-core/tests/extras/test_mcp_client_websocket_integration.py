"""End-to-end integration test for the MCP-over-WebSocket transport.

T2 (config + dispatcher) is verified at unit level in
``test_mcp_client_pool.py``. This file is the highest-fidelity
validation for v1.3's WebSocket branch: an in-process WebSocket MCP
server is spun up on an OS-assigned localhost port, an
:class:`McpClientPool` is pointed at the ``ws://127.0.0.1:<port>/`` URL
via ``transport="websocket"``, and a tool round-trip is exercised
through the full transport stack — the real upstream
``websocket_client`` participates, not a mock.

The test validates the seam between every layer:

- T1's :class:`McpServerConfig` (``transport="websocket"`` + ``url``
  field; ``headers``/``auth`` forbidden by validator).
- T2's :meth:`McpClientPool._spawn_websocket` dispatch.
- The upstream ``mcp.client.websocket.websocket_client`` context
  manager and its 2-tuple ``(read, write)`` shape (no
  ``get_session_id`` callback).
- The shared :meth:`McpClientPool._open_session` path —
  ``ClientSession.initialize()`` + ``list_tools()`` + the
  ``McpClientSession`` wrapper.
- The adapter's :func:`_decode_response` (FastMCP serialises dict
  returns as JSON inside a ``TextContent.text``; the adapter parses
  it back to a dict before returning to the agent layer).

Server-side helper: unlike SSE/Streamable-HTTP, ``FastMCP`` does not
expose a ``websocket_app()`` builder — it only ships ``sse_app()`` and
``streamable_http_app()``. The upstream WebSocket transport lives at
``mcp.server.websocket.websocket_server`` as a low-level ASGI primitive
that takes ``(scope, receive, send)`` directly. We assemble a minimal
ASGI app here that delegates to ``websocket_server`` inside the
handler and drives the underlying ``FastMCP._mcp_server.run`` loop —
this is a few lines and mirrors the pattern ``FastMCP.sse_app`` uses
internally for its own SSE handler.

The assembled ASGI app is registered as uvicorn's top-level app (no
Starlette router in between) because Starlette's ``Mount`` rejects
unauthenticated WebSocket scopes with a 403 — a fixture-shaped
limitation, not a production concern (real users would mount via
their own framework's WebSocket-aware routing).

Port-discovery brittleness: uvicorn's API for "what port did I
actually bind?" reaches into ``server.servers[0].sockets[0]``. That
attribute path is internal-ish; future uvicorn versions may shift it.
This mirrors the same pattern used by the SSE and Streamable-HTTP
integration tests next door.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from typing import Any

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("websockets")

# Mirror v1.2.1's lesson: scoped ``ignore::DeprecationWarning`` silences
# the upstream uvicorn → websockets noise without weakening the SDK's
# strict deprecation policy elsewhere. Same rationale as the SSE and
# HTTP integration tests next door.
pytestmark = [
    pytest.mark.extras,
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from eap_core.mcp.client import McpClientPool, McpServerConfig


@pytest.fixture
async def in_process_mcp_websocket_server() -> AsyncIterator[str]:
    """Spin up an MCP server over WebSocket on a local port.

    Yields the ``ws://`` URL pointing at the running server. Tears down
    uvicorn cleanly on fixture exit.

    Assembly: ``FastMCP`` builds the tool registry / handler stack but
    has no public ``websocket_app()`` helper. We reach into its
    private ``_mcp_server`` (the low-level ``mcp.server.lowlevel.Server``
    instance) to drive the WebSocket transport directly via
    ``mcp.server.websocket.websocket_server``. The whole ASGI app is
    one async function — no Starlette routing, since Starlette's
    ``Mount`` rejects unauthenticated WebSocket scopes with HTTP 403
    in this configuration.
    """
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    from mcp.server.websocket import websocket_server

    mcp_server = FastMCP(name="hello-server-ws")

    @mcp_server.tool(description="Return a static greeting for WebSocket integration testing.")
    async def hello(name: str = "world") -> dict[str, str]:
        return {"greeting": f"hello {name}"}

    @mcp_server.tool(description="Echo back the sum of two integers (WebSocket).")
    async def add(a: int, b: int) -> dict[str, int]:
        return {"sum": a + b}

    async def asgi_ws_app(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        # Ignore non-websocket lifecycle/HTTP scopes; uvicorn sends a
        # ``lifespan`` scope at startup that we silently no-op past.
        if scope["type"] != "websocket":
            return
        async with websocket_server(scope, receive, send) as (read, write):
            await mcp_server._mcp_server.run(
                read,
                write,
                mcp_server._mcp_server.create_initialization_options(),
            )

    config = uvicorn.Config(asgi_ws_app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    # Wait for uvicorn to bind a port and complete startup.
    for _ in range(200):  # ~10s ceiling at 0.05s polls
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - uvicorn startup failure is exceptional
        server.should_exit = True
        await serve_task
        raise RuntimeError("uvicorn did not start within 10s")

    # Port discovery — same internal-ish path as the HTTP/SSE fixtures.
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/"

    try:
        yield url
    finally:
        server.should_exit = True
        await serve_task


async def test_pool_round_trip_against_in_process_websocket_server(
    in_process_mcp_websocket_server: str,
) -> None:
    """Full end-to-end: pool spawns a WebSocket session, lists tools,
    invokes one through the agent-layer registry, decodes the JSON
    payload back to a Python dict.

    This single test exercises every layer T2 added plus the upstream
    ``websocket_client``. If any of those break — config validation
    rejects websocket configs, ``_spawn_websocket`` dispatches wrong,
    the 2-tuple unpack fails, the response decoding regresses — this
    test fails. That's the load-bearing assertion for T2.
    """
    cfg = McpServerConfig(
        name="local-ws",
        transport="websocket",
        url=in_process_mcp_websocket_server,
    )
    async with McpClientPool([cfg]) as pool:
        handles = pool.handles()
        assert len(handles) == 1
        handle = handles[0]
        assert handle.config.transport == "websocket"
        assert handle.config.url == in_process_mcp_websocket_server
        assert set(handle.tool_names) == {"hello", "add"}

        registry = pool.build_tool_registry()
        result = await registry.invoke("local-ws__hello", {"name": "alice"})
        assert result == {"greeting": "hello alice"}


async def test_pool_invokes_multiple_tools_on_same_websocket_server(
    in_process_mcp_websocket_server: str,
) -> None:
    """Second tool invocation on the same WebSocket server confirms the
    forwarder factory pinned the per-tool name correctly — mirrors the
    parallel HTTP/SSE tests, validating the same closure-capture
    invariant across all three remote transports.
    """
    cfg = McpServerConfig(
        name="local-ws",
        transport="websocket",
        url=in_process_mcp_websocket_server,
    )
    async with McpClientPool([cfg]) as pool:
        registry = pool.build_tool_registry()
        hello_result = await registry.invoke("local-ws__hello", {"name": "bob"})
        add_result = await registry.invoke("local-ws__add", {"a": 3, "b": 4})
        assert hello_result == {"greeting": "hello bob"}
        assert add_result == {"sum": 7}


async def test_pool_health_check_against_websocket_server(
    in_process_mcp_websocket_server: str,
) -> None:
    """``health_check`` calls ``list_tools`` over the WebSocket transport.
    Confirms the post-initialise path (not just spawn) survives a
    round-trip — i.e. the session stays usable after ``__aenter__``
    completes.
    """
    cfg = McpServerConfig(
        name="local-ws",
        transport="websocket",
        url=in_process_mcp_websocket_server,
    )
    async with McpClientPool([cfg]) as pool:
        health = await pool.health_check()
        assert health == {"local-ws": True}
