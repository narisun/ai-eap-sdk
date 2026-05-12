"""Tests for McpClientSession — uses a stub upstream so they run on the
non-extras path. The real ``mcp.ClientSession`` is exercised via the
integration test in task 4.

The stub is a plain class with ``async def list_tools / call_tool`` so
duck-typing through ``McpClientSession`` works exactly as it would with
the real upstream — no AsyncMock plumbing required and exception
side-effects are wired in the stub's own body.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from eap_core.mcp.client import (
    McpServerDisconnectedError,
    McpToolInvocationError,
    McpToolTimeoutError,
)
from eap_core.mcp.client.session import McpClientSession


class _StubUpstream:
    """Minimal duck-typed stand-in for ``mcp.ClientSession``.

    Each method's behaviour is controlled by attributes set at
    construction time: success returns the canned response; exceptions
    are raised; ``call_tool_delay_s`` lets the timeout tests run real
    ``asyncio.sleep`` so ``asyncio.wait_for`` sees a genuine timeout.
    """

    def __init__(
        self,
        *,
        list_tools_response: Any = None,
        list_tools_exc: Exception | None = None,
        call_tool_response: Any = None,
        call_tool_exc: Exception | None = None,
        call_tool_delay_s: float = 0.0,
    ) -> None:
        self._list_tools_response = list_tools_response
        self._list_tools_exc = list_tools_exc
        self._call_tool_response = call_tool_response
        self._call_tool_exc = call_tool_exc
        self._call_tool_delay_s = call_tool_delay_s
        self.call_tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        if self._list_tools_exc is not None:
            raise self._list_tools_exc
        return self._list_tools_response

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_tool_calls.append((name, arguments))
        if self._call_tool_delay_s:
            await asyncio.sleep(self._call_tool_delay_s)
        if self._call_tool_exc is not None:
            raise self._call_tool_exc
        return self._call_tool_response


def _make_session(upstream: _StubUpstream, *, timeout_s: float = 1.0) -> McpClientSession:
    return McpClientSession(server_name="test", upstream=upstream, request_timeout_s=timeout_s)


async def test_list_tools_returns_upstream_tools() -> None:
    upstream = _StubUpstream(
        list_tools_response=SimpleNamespace(
            tools=[SimpleNamespace(name="x"), SimpleNamespace(name="y")]
        )
    )
    session = _make_session(upstream)
    tools = await session.list_tools()
    assert [t.name for t in tools] == ["x", "y"]


async def test_list_tools_translates_brokenpipe_to_disconnected() -> None:
    upstream = _StubUpstream(list_tools_exc=BrokenPipeError())
    session = _make_session(upstream)
    with pytest.raises(McpServerDisconnectedError, match="disconnected"):
        await session.list_tools()


async def test_list_tools_translates_eoferror_to_disconnected() -> None:
    """``EOFError`` from the upstream stdio framer must map to the same
    typed error as ``BrokenPipeError`` / ``ConnectionError`` — they all
    mean "the subprocess is gone" from the pool's point of view."""
    upstream = _StubUpstream(list_tools_exc=EOFError("server stream closed"))
    session = _make_session(upstream)
    with pytest.raises(McpServerDisconnectedError, match="disconnected"):
        await session.list_tools()


async def test_call_tool_returns_upstream_response() -> None:
    response = SimpleNamespace(content=[SimpleNamespace(text='{"ok": true}')])
    upstream = _StubUpstream(call_tool_response=response)
    session = _make_session(upstream)
    out = await session.call_tool("list_tables", {})
    assert out.content[0].text == '{"ok": true}'


async def test_call_tool_times_out_when_upstream_exceeds_limit() -> None:
    upstream = _StubUpstream(call_tool_delay_s=0.5)
    session = _make_session(upstream, timeout_s=0.05)
    with pytest.raises(McpToolTimeoutError) as ei:
        await session.call_tool("slow", {})
    assert ei.value.tool == "slow"
    assert ei.value.timeout_s == 0.05


async def test_call_tool_translates_disconnect_during_call() -> None:
    upstream = _StubUpstream(call_tool_exc=BrokenPipeError())
    session = _make_session(upstream)
    with pytest.raises(McpServerDisconnectedError, match="disconnected"):
        await session.call_tool("x", {})


async def test_call_tool_translates_connectionerror_during_call() -> None:
    """``ConnectionError`` is the third member of the disconnect-family
    tuple — covers the branch alongside ``BrokenPipeError`` /
    ``EOFError`` for environments that surface a generic socket error
    instead of the stdio-specific subclasses.
    """
    upstream = _StubUpstream(call_tool_exc=ConnectionError("reset"))
    session = _make_session(upstream)
    with pytest.raises(McpServerDisconnectedError, match="disconnected"):
        await session.call_tool("x", {})


async def test_call_tool_translates_unknown_upstream_exception_to_invocation_error() -> None:
    upstream = _StubUpstream(call_tool_exc=RuntimeError("server kaboom"))
    session = _make_session(upstream)
    with pytest.raises(McpToolInvocationError, match="kaboom"):
        await session.call_tool("x", {})


async def test_session_forwards_arguments_to_upstream() -> None:
    upstream = _StubUpstream(call_tool_response=SimpleNamespace(content=[]))
    session = _make_session(upstream)
    await session.call_tool("query_sql", {"sql": "SELECT 1", "limit": 10})
    assert upstream.call_tool_calls == [("query_sql", {"sql": "SELECT 1", "limit": 10})]


async def test_session_name_property_reflects_constructor_arg() -> None:
    upstream = _StubUpstream(
        list_tools_response=SimpleNamespace(tools=[]),
    )
    session = McpClientSession(server_name="bankdw", upstream=upstream, request_timeout_s=1.0)
    assert session.name == "bankdw"
