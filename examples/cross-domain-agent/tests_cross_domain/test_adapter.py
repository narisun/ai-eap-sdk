"""Example-level tests for the v1.1 SDK MCP client adapter.

v1.1 replaced the per-agent shim that used to live in
``mcp_client_adapter.py`` with the first-class SDK adapter at
:mod:`eap_core.mcp.client.adapter`. The unit tests for the adapter itself
live alongside the SDK in
``packages/eap-core/tests/test_mcp_client_adapter.py`` — that's the
canonical surface and where the load-bearing closure-capture mutation
guard lives.

These example-level tests stay around for two reasons:

1. They prove the v1.0 → v1.1 **compat shim** (``mcp_client_adapter.py``
   next to this test file) still preserves the v1.0 public signatures
   (``connect_servers`` / ``build_tool_specs`` / ``ServerHandle``) by
   delegating to the SDK. Anyone who pinned to the v1.0 shim should be
   able to upgrade without code changes.
2. They demonstrate the **canonical v1.1 pattern** using
   :class:`McpClientPool` directly, so reviewers comparing the v1.0 and
   v1.1 example test suites see what the migration looks like in one
   file.

The integration test (``test_agent.py``) is the headline end-to-end
proof; these unit-level tests stay quick (no real subprocess spawn).
"""

from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from mcp_client_adapter import ServerHandle, build_tool_specs, connect_servers

from eap_core.mcp.client import (
    McpServerConfig,
    McpServerDisconnectedError,
    McpServerHandle,
)
from eap_core.mcp.client.adapter import build_tool_registry
from eap_core.mcp.client.session import McpClientSession

# ---------------------------------------------------------------------------
# v1.0 compat-shim tests — confirm legacy callers still work
# ---------------------------------------------------------------------------


class _StubResponse:
    """Minimal mcp.types.CallToolResult lookalike."""

    def __init__(self, *, text: str | None, has_content: bool = True) -> None:
        if not has_content:
            self.content: list = []
        elif text is None:
            self.content = [SimpleNamespace()]  # no .text attr
        else:
            self.content = [SimpleNamespace(text=text)]


def _make_compat_handle(
    *, server_name: str, tool_names: list[str], session: AsyncMock
) -> ServerHandle:
    """Build a ``ServerHandle`` (now alias of ``McpServerHandle``) the
    way the v1.0 caller would: just name + session + tool_names. The
    config defaults are fine since the v1.0 surface didn't expose them.
    """
    return ServerHandle(
        config=McpServerConfig(name=server_name, command="x"),
        session=session,
        tool_names=tool_names,
    )


@pytest.mark.asyncio
async def test_v1_compat_build_tool_specs_namespaces_tools_per_server() -> None:
    """v1.0 shim contract: ``build_tool_specs`` namespaces each remote
    tool as ``<server>__<tool>``. v1.1 routes that through the SDK
    adapter; the externally-observable shape is identical.
    """
    h1 = _make_compat_handle(
        server_name="bankdw",
        tool_names=["query_sql", "list_tables"],
        session=AsyncMock(),
    )
    h2 = _make_compat_handle(
        server_name="sfcrm",
        tool_names=["query_sql"],
        session=AsyncMock(),
    )
    specs = build_tool_specs([h1, h2])
    names = sorted(s.name for s in specs)
    assert names == [
        "bankdw__list_tables",
        "bankdw__query_sql",
        "sfcrm__query_sql",
    ]


@pytest.mark.asyncio
async def test_v1_compat_forwarder_invokes_correct_remote_tool_with_kwargs() -> None:
    """The closure-capture guard, re-asserted at the shim level. If the
    SDK adapter ever regressed the per-iteration binding, this would
    catch it via the v1.0 entry points too.
    """
    session = AsyncMock()
    session.call_tool = AsyncMock(return_value=_StubResponse(text=json.dumps({"row_count": 7})))
    h = _make_compat_handle(
        server_name="bankdw",
        tool_names=["query_sql", "list_tables"],
        session=session,
    )
    specs = build_tool_specs([h])

    list_spec = next(s for s in specs if s.name == "bankdw__list_tables")
    await list_spec.fn()
    session.call_tool.assert_called_with("list_tables", {})

    session.call_tool.reset_mock()
    query_spec = next(s for s in specs if s.name == "bankdw__query_sql")
    result = await query_spec.fn(sql="SELECT 1", limit=10)
    session.call_tool.assert_called_with("query_sql", {"sql": "SELECT 1", "limit": 10})
    assert result == {"row_count": 7}


@pytest.mark.asyncio
async def test_v1_compat_serverhandle_is_alias_for_mcp_server_handle() -> None:
    """``ServerHandle`` in the shim is now a direct alias for
    :class:`McpServerHandle`. Construct one through the v1.0 shim and
    confirm ``isinstance`` against the SDK class — a tiny test that
    catches accidental copy-paste forks of the dataclass.
    """
    h = _make_compat_handle(server_name="x", tool_names=["t"], session=AsyncMock())
    assert isinstance(h, McpServerHandle)


@pytest.mark.asyncio
async def test_v1_compat_connect_servers_returns_empty_on_empty_input() -> None:
    """L2 (v1.2): an empty server-config list returns ``[]`` rather
    than constructing a pool (which would raise ``ValueError`` because
    :class:`McpClientPool` rejects empty config lists). The v1.0 shim
    signature accepted "no servers" as valid; preserving that contract
    keeps callers that may legitimately have no servers configured
    (e.g. environment-gated rollouts) from crashing at startup.
    """
    async with AsyncExitStack() as stack:
        handles = await connect_servers([], stack)
        assert handles == []


@pytest.mark.asyncio
async def test_v1_compat_shim_pool_reconnect_is_noop_and_lets_disconnect_propagate() -> None:
    """L4 (v1.2): the shim's ``_LooseHandlesPool.reconnect`` is a no-op
    so the SDK adapter's forwarder

        except McpServerDisconnectedError:
            await pool.reconnect(server_name)
            raise

    can complete without crashing, and the original
    :class:`McpServerDisconnectedError` then propagates to the caller —
    the same shape v1.0 callers saw (v1.0 had no reconnect concept).

    Previously the shim raised ``RuntimeError`` here, which would have
    masked the disconnect error and broken any caller relying on the
    typed error to detect "server went away".
    """
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=McpServerDisconnectedError("server went away"))
    h = _make_compat_handle(
        server_name="bankdw",
        tool_names=["query_sql"],
        session=session,
    )
    specs = build_tool_specs([h])
    spec = next(s for s in specs if s.name == "bankdw__query_sql")

    # The forwarder catches the disconnect, calls the shim pool's
    # ``reconnect`` (which is now a no-op rather than raising
    # RuntimeError), and re-raises the original disconnect error.
    with pytest.raises(McpServerDisconnectedError, match="server went away"):
        await spec.fn(sql="SELECT 1")


# ---------------------------------------------------------------------------
# v1.1 SDK tests — exercise the canonical pattern (McpClientPool +
# build_tool_registry). These cover what new code should look like.
# ---------------------------------------------------------------------------


class _RecordingUpstream:
    """Stub for the upstream ``mcp.ClientSession``: records every
    ``call_tool`` invocation and replays scripted responses per tool name.
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        return self.responses.get(name, SimpleNamespace(content=[]))


class _FakePool:
    """Minimal stand-in for :class:`McpClientPool` used to drive the
    SDK adapter without spawning real subprocesses. Implements the three
    methods the adapter actually touches (``handles`` / ``session`` /
    ``reconnect``).
    """

    def __init__(self, handles: list[McpServerHandle]) -> None:
        self._by_name = {h.config.name: h for h in handles}
        self._order = [h.config.name for h in handles]
        self.reconnect_calls: list[str] = []

    def handles(self) -> list[McpServerHandle]:
        return [self._by_name[n] for n in self._order]

    def session(self, server_name: str) -> McpClientSession:
        return self._by_name[server_name].session

    async def reconnect(self, server_name: str) -> None:
        self.reconnect_calls.append(server_name)


def _make_sdk_handle(
    *, server_name: str, tool_names: list[str], upstream: _RecordingUpstream
) -> McpServerHandle:
    return McpServerHandle(
        config=McpServerConfig(name=server_name, command="x"),
        session=McpClientSession(
            server_name=server_name,
            upstream=upstream,
            request_timeout_s=5.0,
        ),
        tool_names=tool_names,
    )


@pytest.mark.asyncio
async def test_sdk_pattern_build_tool_registry_namespaces_each_server() -> None:
    """The v1.1 canonical pattern: build a pool, call
    ``build_tool_registry``, get a populated registry. Confirms the SDK
    adapter produces the same namespaced names the v1.0 shim did.
    """
    u_a = _RecordingUpstream()
    u_b = _RecordingUpstream()
    h_a = _make_sdk_handle(server_name="bankdw", tool_names=["query_sql"], upstream=u_a)
    h_b = _make_sdk_handle(server_name="sfcrm", tool_names=["query_sql"], upstream=u_b)
    pool = _FakePool([h_a, h_b])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    names = {spec.name for spec in registry.list_tools()}
    assert names == {"bankdw__query_sql", "sfcrm__query_sql"}


@pytest.mark.asyncio
async def test_sdk_pattern_forwarder_routes_to_correct_server_session() -> None:
    """The two-servers companion to the closure-capture guard. Each
    forwarder must route to its OWN server's session.
    """
    u_a = _RecordingUpstream()
    u_b = _RecordingUpstream()
    u_a.responses = {"ping": SimpleNamespace(content=[SimpleNamespace(text='"pong-a"')])}
    u_b.responses = {"ping": SimpleNamespace(content=[SimpleNamespace(text='"pong-b"')])}
    h_a = _make_sdk_handle(server_name="bankdw", tool_names=["ping"], upstream=u_a)
    h_b = _make_sdk_handle(server_name="sfcrm", tool_names=["ping"], upstream=u_b)
    pool = _FakePool([h_a, h_b])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    a_result = await registry.invoke("bankdw__ping", {})
    b_result = await registry.invoke("sfcrm__ping", {})

    assert a_result == "pong-a"
    assert b_result == "pong-b"
    assert u_a.calls == [("ping", {})]
    assert u_b.calls == [("ping", {})]
