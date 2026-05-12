"""End-to-end integration test for the legacy SSE MCP transport.

T1 (config + dispatcher) was tested with mocked upstreams. This file
is the highest-fidelity validation for v1.3's SSE branch: an
in-process MCP SSE server is spun up on an OS-assigned localhost port,
an :class:`McpClientPool` is pointed at its ``/sse`` URL via
``transport="sse"``, and a tool round-trip is exercised through the
full transport stack — the real upstream ``sse_client`` participates,
not a mock.

The test validates the seam between every layer:

- T1's :class:`McpServerConfig` (``transport="sse"`` + ``url`` field).
- T1's :meth:`McpClientPool._spawn_sse` dispatch.
- The upstream ``mcp.client.sse.sse_client`` context manager and its
  2-tuple ``(read, write)`` shape (no ``get_session_id`` callback).
- The shared :meth:`McpClientPool._open_session` path —
  ``ClientSession.initialize()`` + ``list_tools()`` + the
  ``McpClientSession`` wrapper.
- The adapter's :func:`_decode_response` (FastMCP serialises dict
  returns as JSON inside a ``TextContent.text``; the adapter parses
  it back to a dict before returning to the agent layer).

Server-side helper: ``mcp.server.fastmcp.FastMCP`` exposes ``sse_app()``
which returns a Starlette app with a ``/sse`` GET endpoint and a
``/messages/`` POST mount (the canonical SSE two-route layout). The
SSE client targets the ``/sse`` URL; the upstream handles the
message-back POST flow internally.

Port-discovery brittleness: uvicorn's API for "what port did I
actually bind?" reaches into ``server.servers[0].sockets[0]``. That
attribute path is internal-ish; future uvicorn versions may shift it.
This mirrors the same pattern used by the Streamable-HTTP integration
test in ``test_mcp_client_http_integration.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

# Mirror v1.2.1's lesson: scoped ``ignore::DeprecationWarning`` silences
# the upstream uvicorn → websockets noise without weakening the SDK's
# strict deprecation policy elsewhere. Same rationale as the HTTP
# integration test next door.
pytestmark = [
    pytest.mark.extras,
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from eap_core.mcp.client import McpClientPool, McpServerConfig


@pytest.fixture
async def in_process_mcp_sse_server() -> AsyncIterator[str]:
    """Spin up an MCP server over legacy SSE on a local port.

    Yields the ``/sse`` URL pointing at the running server. Tears down
    uvicorn cleanly on fixture exit.

    ``FastMCP.sse_app()`` returns a Starlette app with a ``/sse`` GET
    endpoint plus a ``/messages/`` POST mount; the SSE client targets
    the ``/sse`` URL and the upstream handles the message-back POST
    flow internally. Same uvicorn lifecycle pattern as the
    Streamable-HTTP integration test.
    """
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp_server = FastMCP(name="hello-server-sse")

    @mcp_server.tool(description="Return a static greeting for SSE integration testing.")
    async def hello(name: str = "world") -> dict[str, str]:
        return {"greeting": f"hello {name}"}

    @mcp_server.tool(description="Echo back the sum of two integers (SSE).")
    async def add(a: int, b: int) -> dict[str, int]:
        return {"sum": a + b}

    app = mcp_server.sse_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
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

    # Port discovery — same internal-ish path as the HTTP fixture.
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/sse"

    try:
        yield url
    finally:
        server.should_exit = True
        await serve_task


async def test_pool_round_trip_against_in_process_sse_server(
    in_process_mcp_sse_server: str,
) -> None:
    """Full end-to-end: pool spawns an SSE session, lists tools,
    invokes one through the agent-layer registry, decodes the JSON
    payload back to a Python dict.

    This single test exercises every layer T1 added plus the upstream
    ``sse_client``. If any of those break — config validation rejects
    sse configs, ``_spawn_sse`` dispatches wrong, the 2-tuple unpack
    fails, the response decoding regresses — this test fails. That's
    the load-bearing assertion for T1.
    """
    cfg = McpServerConfig(
        name="local-sse",
        transport="sse",
        url=in_process_mcp_sse_server,
    )
    async with McpClientPool([cfg]) as pool:
        handles = pool.handles()
        assert len(handles) == 1
        handle = handles[0]
        assert handle.config.transport == "sse"
        assert handle.config.url == in_process_mcp_sse_server
        assert set(handle.tool_names) == {"hello", "add"}

        registry = pool.build_tool_registry()
        result = await registry.invoke("local-sse__hello", {"name": "alice"})
        assert result == {"greeting": "hello alice"}


async def test_pool_invokes_multiple_tools_on_same_sse_server(
    in_process_mcp_sse_server: str,
) -> None:
    """Second tool invocation on the same SSE server confirms the
    forwarder factory pinned the per-tool name correctly — mirrors the
    parallel HTTP test, validating the same closure-capture invariant
    across both remote transports.
    """
    cfg = McpServerConfig(
        name="local-sse",
        transport="sse",
        url=in_process_mcp_sse_server,
    )
    async with McpClientPool([cfg]) as pool:
        registry = pool.build_tool_registry()
        hello_result = await registry.invoke("local-sse__hello", {"name": "bob"})
        add_result = await registry.invoke("local-sse__add", {"a": 3, "b": 4})
        assert hello_result == {"greeting": "hello bob"}
        assert add_result == {"sum": 7}


async def test_pool_health_check_against_sse_server(
    in_process_mcp_sse_server: str,
) -> None:
    """``health_check`` calls ``list_tools`` over the SSE transport.
    Confirms the post-initialise path (not just spawn) survives a
    round-trip — i.e. the session stays usable after ``__aenter__``
    completes.
    """
    cfg = McpServerConfig(
        name="local-sse",
        transport="sse",
        url=in_process_mcp_sse_server,
    )
    async with McpClientPool([cfg]) as pool:
        health = await pool.health_check()
        assert health == {"local-sse": True}
