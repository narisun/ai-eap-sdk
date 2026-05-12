"""End-to-end integration test for the Streamable-HTTP MCP transport.

T1 (config) and T2 (pool spawn branching) were tested with mocked
upstreams. This file is the highest-fidelity validation for v1.2: an
in-process MCP HTTP server is spun up on an OS-assigned localhost port,
an :class:`McpClientPool` is pointed at its URL via
``transport="http"``, and a tool round-trip is exercised through the
full transport stack — the real upstream ``streamable_http_client``
participates, not a mock.

The test validates the seam between every layer:

- T1's :class:`McpServerConfig` (``transport="http"`` + ``url`` field).
- T2's :meth:`McpClientPool._spawn_http` dispatch.
- The upstream ``mcp.client.streamable_http.streamable_http_client``
  context manager and its 3-tuple ``(read, write, get_session_id)``
  shape; the pool drops the third element.
- The shared :meth:`McpClientPool._open_session` path —
  ``ClientSession.initialize()`` + ``list_tools()`` + the
  ``McpClientSession`` wrapper.
- The adapter's :func:`_decode_response` (FastMCP serialises dict
  returns as JSON inside a ``TextContent.text``; the adapter parses
  it back to a dict before returning to the agent layer).

Server-side helper: ``mcp.server.fastmcp.FastMCP`` is the canonical
ASGI app builder for Streamable-HTTP in the pinned ``mcp`` version.
Its ``streamable_http_app()`` returns a Starlette app that mounts the
StreamableHTTPSessionManager at ``/mcp`` by default — matching the URL
shape the client connects to.

Port-discovery brittleness: uvicorn's API for "what port did I
actually bind?" reaches into ``server.servers[0].sockets[0]``. That
attribute path is internal-ish; future uvicorn versions may shift it.
If the fixture starts failing with ``AttributeError`` on that chain,
look for the equivalent on whatever shape the new uvicorn exposes
``server.servers`` as.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

# v1.2.1: uvicorn (a transitive dep of this test's in-process MCP server)
# eagerly imports several modules from the ``websockets`` package whose
# transition to the newer asyncio-native API emits ``DeprecationWarning``s
# at module-load time. The repo's
# ``filterwarnings = ["error::DeprecationWarning"]`` policy escalates
# those to test failures, blocking the fixture before any test runs.
#
# The warnings live entirely in upstream code we don't own (uvicorn →
# websockets.legacy, websockets.imports). Scoping a broad
# ``ignore::DeprecationWarning`` to this single test file silences the
# upstream noise without weakening the SDK's strict deprecation policy
# everywhere else. The trade-off: a real SDK-emitted deprecation that
# happened to fire inside one of THESE three tests would also be
# silenced — accepted because the tests here only exercise a single
# narrow integration seam and no SDK-emitted deprecations are expected
# in that path.
pytestmark = [
    pytest.mark.extras,
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from eap_core.mcp.client import McpClientPool, McpServerConfig


@pytest.fixture
async def in_process_mcp_server() -> AsyncIterator[str]:
    """Spin up an MCP server over Streamable-HTTP on a local port.

    Yields the ``/mcp`` URL pointing at the running server. Tears down
    uvicorn cleanly on fixture exit.

    Why ``FastMCP`` rather than ``eap_core.mcp.server.build_mcp_server``:
    the eap-core builder returns the low-level ``mcp.server.Server``
    object, which is the right shape to feed into
    ``StreamableHTTPSessionManager`` directly — but composing that
    by hand requires an ASGI lifespan, manual route registration, and
    shutdown wiring. FastMCP wraps all of that in
    ``streamable_http_app()``. Either path validates the same client
    seam; FastMCP is far less fragile to upstream MCP version drift
    because it owns the lifecycle internally.

    Port = 0 asks the OS for any free port; we read the actually-bound
    port after uvicorn calls ``socket.bind()``. ``server.started`` is
    uvicorn's public-ish "lifespan complete" flag — polling it avoids a
    race where the fixture yields before the port is actually
    accepting connections.
    """
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    mcp_server = FastMCP(name="hello-server")

    @mcp_server.tool(description="Return a static greeting for integration testing.")
    async def hello(name: str = "world") -> dict[str, str]:
        return {"greeting": f"hello {name}"}

    @mcp_server.tool(description="Echo back the sum of two integers.")
    async def add(a: int, b: int) -> dict[str, int]:
        return {"sum": a + b}

    app = mcp_server.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    # Wait for uvicorn to bind a port and complete startup. ``started``
    # is set inside ``Server.startup()`` once every socket is open;
    # polling avoids a race where ``yield`` returns the URL before any
    # ``connect()`` would succeed.
    for _ in range(200):  # ~10s ceiling at 0.05s polls
        if server.started:
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover - uvicorn startup failure is exceptional
        server.should_exit = True
        await serve_task
        raise RuntimeError("uvicorn did not start within 10s")

    # Port discovery — uvicorn ``Server`` exposes the bound sockets via
    # ``server.servers[0].sockets[0]``. This is an internal-ish path
    # (the public ``Server.config.port`` would still be 0). If a future
    # uvicorn refactors this away, the fixture will need to use whatever
    # the new equivalent is.
    port = server.servers[0].sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"

    try:
        yield url
    finally:
        server.should_exit = True
        # Give uvicorn a moment to drain in-flight connections; the
        # ``await serve_task`` then unwinds the lifespan.
        await serve_task


async def test_pool_round_trip_against_in_process_http_server(
    in_process_mcp_server: str,
) -> None:
    """Full end-to-end: pool spawns an HTTP session, lists tools,
    invokes one through the agent-layer registry, decodes the JSON
    payload back to a Python dict.

    This single test exercises every layer T1+T2 added plus the
    upstream ``streamablehttp_client``. If any of those break — config
    validation rejects http configs, ``_spawn_http`` dispatches wrong,
    the 3-tuple unpack fails, the response decoding regresses — this
    test fails. That's the load-bearing assertion for v1.2.
    """
    cfg = McpServerConfig(
        name="local-http",
        transport="http",
        url=in_process_mcp_server,
    )
    async with McpClientPool([cfg]) as pool:
        # Sanity: the http path constructed a handle with the right
        # config + the tool names advertised over the wire.
        handles = pool.handles()
        assert len(handles) == 1
        handle = handles[0]
        assert handle.config.transport == "http"
        assert handle.config.url == in_process_mcp_server
        assert set(handle.tool_names) == {"hello", "add"}

        # Round-trip via the agent-layer tool registry: namespaced
        # ``<server>__<tool>`` lookup → forwarder → ``call_tool`` →
        # decoded JSON dict back to the caller.
        registry = pool.build_tool_registry()
        result = await registry.invoke("local-http__hello", {"name": "alice"})
        assert result == {"greeting": "hello alice"}


async def test_pool_invokes_multiple_tools_on_same_http_server(
    in_process_mcp_server: str,
) -> None:
    """Second tool invocation on the same server confirms the
    forwarder factory pinned the per-tool name correctly (the
    closure-capture bug guarded against in
    ``test_forwarder_invokes_correct_remote_tool_with_kwargs`` —
    re-validated end-to-end here against a real server).
    """
    cfg = McpServerConfig(
        name="local-http",
        transport="http",
        url=in_process_mcp_server,
    )
    async with McpClientPool([cfg]) as pool:
        registry = pool.build_tool_registry()
        hello_result = await registry.invoke("local-http__hello", {"name": "bob"})
        add_result = await registry.invoke("local-http__add", {"a": 3, "b": 4})
        assert hello_result == {"greeting": "hello bob"}
        assert add_result == {"sum": 7}


async def test_pool_health_check_against_http_server(
    in_process_mcp_server: str,
) -> None:
    """``health_check`` calls ``list_tools`` over the HTTP transport.
    Confirms the post-initialise path (not just spawn) survives a
    round-trip — i.e. the session stays usable after ``__aenter__``
    completes.
    """
    cfg = McpServerConfig(
        name="local-http",
        transport="http",
        url=in_process_mcp_server,
    )
    async with McpClientPool([cfg]) as pool:
        health = await pool.health_check()
        assert health == {"local-http": True}
