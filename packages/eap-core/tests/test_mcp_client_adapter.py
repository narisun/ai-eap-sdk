"""Tests for the MCP client adapter — ``build_tool_registry`` + forwarder.

The adapter sits between a live :class:`McpClientPool` and the local
:class:`McpToolRegistry`. Its job is to turn each remote tool into a
namespaced :class:`ToolSpec` forwarder that calls back through the pool.

These tests fabricate a minimal pool-like object (duck-typed —
``handles()`` + ``session(name)`` + ``reconnect(name)``) so the adapter can
be exercised without ``mcp`` installed. The same closure-capture mutation
guard the v1.0 example shim shipped with is preserved here as the
load-bearing test:

    test_forwarder_invokes_correct_remote_tool_with_kwargs

Verifies the per-iteration values bind correctly by building forwarders
for TWO tools on the same server and checking each forwarder calls its
OWN tool. The inline-loop bug would make both forwarders call the LAST
tool; this test catches that mutation.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from eap_core.mcp.client import McpServerConfig, McpServerDisconnectedError, McpServerHandle
from eap_core.mcp.client.adapter import build_tool_registry
from eap_core.mcp.client.session import McpClientSession


class _RecordingUpstream:
    """Records every ``call_tool`` invocation and replays scripted responses.

    Tests configure ``responses`` as a per-tool-name dict mapping to the
    upstream-shaped ``SimpleNamespace(content=[...])`` value. Tests that
    need to script a disconnect set ``call_tool_exc`` (raised before
    the response is consulted).
    """

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.call_tool_exc: Exception | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if self.call_tool_exc is not None:
            raise self.call_tool_exc
        return self.responses.get(name, SimpleNamespace(content=[]))


class _FakePool:
    """Minimal duck-typed stand-in for :class:`McpClientPool`.

    Implements the surface the adapter actually uses:
    ``handles()`` / ``session(name)`` / ``reconnect(name)``.
    """

    def __init__(self, handles: list[McpServerHandle]) -> None:
        self._handles_by_name = {h.config.name: h for h in handles}
        self._order = [h.config.name for h in handles]
        self.reconnect_calls: list[str] = []

    def handles(self) -> list[McpServerHandle]:
        return [self._handles_by_name[n] for n in self._order]

    def session(self, server_name: str) -> McpClientSession:
        return self._handles_by_name[server_name].session

    async def reconnect(self, server_name: str) -> None:
        self.reconnect_calls.append(server_name)
        # The test's contract is "reconnect was called"; we don't need
        # to actually spawn a new session for the assertion to hold.


def _make_handle(
    *,
    server_name: str,
    tool_names: list[str],
    upstream: _RecordingUpstream,
) -> McpServerHandle:
    cfg = McpServerConfig(name=server_name, command="x")
    session = McpClientSession(
        server_name=server_name,
        upstream=upstream,
        request_timeout_s=5.0,
    )
    return McpServerHandle(config=cfg, session=session, tool_names=tool_names)


# ---------------------------------------------------------------------------
# Namespace prefixing
# ---------------------------------------------------------------------------


async def test_build_tool_registry_namespaces_tools_per_server() -> None:
    """Two servers each exposing ``query_sql`` must produce two distinct
    namespaced tool names. This is why namespacing exists in the first
    place — the v1.0 example demonstrated the collision by spawning both
    bankdw and sfcrm, which each expose a ``query_sql`` of their own.
    """
    u_a = _RecordingUpstream()
    u_b = _RecordingUpstream()
    handle_a = _make_handle(
        server_name="bankdw", tool_names=["list_tables", "query_sql"], upstream=u_a
    )
    handle_b = _make_handle(
        server_name="sfcrm", tool_names=["list_tables", "query_sql"], upstream=u_b
    )
    pool = _FakePool([handle_a, handle_b])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    names = {spec.name for spec in registry.list_tools()}
    assert names == {
        "bankdw__list_tables",
        "bankdw__query_sql",
        "sfcrm__list_tables",
        "sfcrm__query_sql",
    }


async def test_forwarder_description_includes_remote_marker() -> None:
    """The ``[remote: <server>] <tool>`` description prefix helps a
    downstream LLM tool-picker distinguish remote forwarders from local
    tools at a glance. The v1.0 shim shipped with this convention; the
    SDK preserves it."""
    upstream = _RecordingUpstream()
    handle = _make_handle(server_name="bankdw", tool_names=["query_sql"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    spec = registry.get("bankdw__query_sql")
    assert spec is not None
    assert spec.description == "[remote: bankdw] query_sql"


# ---------------------------------------------------------------------------
# Closure capture — the load-bearing mutation guard
# ---------------------------------------------------------------------------


async def test_forwarder_invokes_correct_remote_tool_with_kwargs() -> None:
    """LOAD-BEARING MUTATION GUARD.

    Build forwarders for TWO tools on the SAME server. Then call each
    forwarder with distinct kwargs. If the adapter inlines the
    ``async def _forward`` inside the ``for`` loop instead of using the
    ``_build_forwarder_spec`` factory, the closure captures the LOOP
    variable, and BOTH forwarders end up calling the LAST tool registered
    (``query_sql``). This test would fail because the recorded calls
    would show ``[("query_sql", ...), ("query_sql", ...)]`` instead of
    ``[("list_tables", ...), ("query_sql", ...)]``.

    The factory pattern in ``_build_forwarder_spec`` pins the per-tool
    values via function parameters, so each forwarder closes over its
    own values and the recorded calls show the correct tool names.
    """
    upstream = _RecordingUpstream()
    upstream.responses = {
        "list_tables": SimpleNamespace(
            content=[SimpleNamespace(text='{"tables": [{"name": "dim_party"}]}')]
        ),
        "query_sql": SimpleNamespace(content=[SimpleNamespace(text='{"rows": [{"id": 1}]}')]),
    }
    handle = _make_handle(
        server_name="bankdw", tool_names=["list_tables", "query_sql"], upstream=upstream
    )
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    list_spec = registry.get("bankdw__list_tables")
    query_spec = registry.get("bankdw__query_sql")
    assert list_spec is not None
    assert query_spec is not None

    list_result = await list_spec.fn()
    query_result = await query_spec.fn(sql="SELECT 1", limit=10)

    # Each forwarder must have called its OWN tool, not the last one.
    # If the closure-capture bug were present, both entries would be
    # ("query_sql", ...) and `list_result` would equal `query_result`.
    assert upstream.calls == [
        ("list_tables", {}),
        ("query_sql", {"sql": "SELECT 1", "limit": 10}),
    ]
    assert list_result == {"tables": [{"name": "dim_party"}]}
    assert query_result == {"rows": [{"id": 1}]}


async def test_closure_capture_holds_across_two_servers() -> None:
    """Belt-and-suspenders companion to the same-server test above.
    Builds one forwarder on each of two servers and confirms each
    forwarder calls its OWN server's session. The inline-loop bug would
    route both calls through the LAST server (sfcrm).
    """
    u_a = _RecordingUpstream()
    u_b = _RecordingUpstream()
    u_a.responses = {"ping": SimpleNamespace(content=[SimpleNamespace(text='"pong-a"')])}
    u_b.responses = {"ping": SimpleNamespace(content=[SimpleNamespace(text='"pong-b"')])}
    handle_a = _make_handle(server_name="bankdw", tool_names=["ping"], upstream=u_a)
    handle_b = _make_handle(server_name="sfcrm", tool_names=["ping"], upstream=u_b)
    pool = _FakePool([handle_a, handle_b])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    a_result = await registry.invoke("bankdw__ping", {})
    b_result = await registry.invoke("sfcrm__ping", {})

    assert a_result == "pong-a"
    assert b_result == "pong-b"
    assert u_a.calls == [("ping", {})]
    assert u_b.calls == [("ping", {})]


# ---------------------------------------------------------------------------
# kwargs forwarding
# ---------------------------------------------------------------------------


async def test_forwarder_forwards_kwargs_unchanged_to_call_tool() -> None:
    """The forwarder accepts ``**kwargs`` and passes them verbatim to
    ``session.call_tool`` as the ``arguments`` dict. No marshalling,
    no key rewriting — the remote server validates."""
    upstream = _RecordingUpstream()
    upstream.responses["query_sql"] = SimpleNamespace(
        content=[SimpleNamespace(text='{"rows": []}')]
    )
    handle = _make_handle(server_name="bankdw", tool_names=["query_sql"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    spec = registry.get("bankdw__query_sql")
    assert spec is not None
    await spec.fn(sql="SELECT * FROM x", limit=50, columns=["a", "b"])

    assert upstream.calls == [
        ("query_sql", {"sql": "SELECT * FROM x", "limit": 50, "columns": ["a", "b"]}),
    ]


# ---------------------------------------------------------------------------
# Disconnect handling
# ---------------------------------------------------------------------------


async def test_forwarder_reconnects_on_disconnect_then_reraises() -> None:
    """When the session raises ``McpServerDisconnectedError``, the
    forwarder must (a) call ``pool.reconnect(server_name)`` so the next
    call routes through a fresh session, and (b) re-raise so the caller
    decides whether to retry. Auto-retry is explicitly deferred to v1.2.
    """
    upstream = _RecordingUpstream()
    upstream.call_tool_exc = BrokenPipeError("stream closed mid-call")
    handle = _make_handle(server_name="bankdw", tool_names=["query_sql"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    spec = registry.get("bankdw__query_sql")
    assert spec is not None

    with pytest.raises(McpServerDisconnectedError):
        await spec.fn(sql="SELECT 1")
    assert pool.reconnect_calls == ["bankdw"]


async def test_forwarder_does_not_reconnect_on_non_disconnect_errors() -> None:
    """A timeout or invocation error is NOT a signal to reconnect — the
    session is still healthy from the pool's point of view. The forwarder
    must let those errors propagate without touching ``reconnect``.
    """
    upstream = _RecordingUpstream()
    upstream.call_tool_exc = RuntimeError("tool raised something else")
    handle = _make_handle(server_name="bankdw", tool_names=["query_sql"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    spec = registry.get("bankdw__query_sql")
    assert spec is not None

    # The session wraps the RuntimeError as McpToolInvocationError; the
    # forwarder propagates without touching ``reconnect``. We catch
    # broadly here because the exact type comes from the session layer
    # and is exercised in test_mcp_client_session; our only assertion is
    # "reconnect was NOT called."
    with pytest.raises(Exception):
        await spec.fn()
    assert pool.reconnect_calls == []


# ---------------------------------------------------------------------------
# Response decoding
# ---------------------------------------------------------------------------


async def test_forwarder_decodes_json_text_content() -> None:
    """The common case: a TextContent whose ``.text`` is a JSON object
    (BaseModel / dict / list serialised by the v0.7.1 server-side fix)
    must decode to the parsed Python value.
    """
    payload = {"tables": [{"name": "dim_party", "rows": 1000}]}
    upstream = _RecordingUpstream()
    upstream.responses["list_tables"] = SimpleNamespace(
        content=[SimpleNamespace(text=json.dumps(payload))]
    )
    handle = _make_handle(server_name="bankdw", tool_names=["list_tables"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    result = await registry.invoke("bankdw__list_tables", {})
    assert result == payload


async def test_forwarder_falls_back_to_raw_text_for_primitive_returns() -> None:
    """v0.7.1 contract: primitive str/int returns are ``str()``-cast
    server-side and won't parse as JSON. The forwarder must fall back to
    returning the raw text rather than raising ``JSONDecodeError``.
    """
    upstream = _RecordingUpstream()
    upstream.responses["ping"] = SimpleNamespace(content=[SimpleNamespace(text="hello world")])
    handle = _make_handle(server_name="srv", tool_names=["ping"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    result = await registry.invoke("srv__ping", {})
    assert result == "hello world"


async def test_forwarder_returns_none_when_content_is_empty() -> None:
    """A tool that returns no content (empty ``response.content`` list)
    is represented as ``None`` at the agent level. Matches the v0.7.1
    example shim's behaviour."""
    upstream = _RecordingUpstream()
    upstream.responses["noop"] = SimpleNamespace(content=[])
    handle = _make_handle(server_name="srv", tool_names=["noop"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    result = await registry.invoke("srv__noop", {})
    assert result is None


async def test_forwarder_passes_through_non_text_content() -> None:
    """ImageContent / EmbeddedResource don't have ``.text``. The forwarder
    passes the raw upstream object through so the agent layer can inspect
    ``response.content`` for rich types. v1.1 explicitly scopes rich
    content decoding OUT.
    """
    image_blob = SimpleNamespace(type="image", data="base64-blob", mimeType="image/png")
    upstream = _RecordingUpstream()
    upstream.responses["render"] = SimpleNamespace(content=[image_blob])
    handle = _make_handle(server_name="srv", tool_names=["render"], upstream=upstream)
    pool = _FakePool([handle])

    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    result = await registry.invoke("srv__render", {})
    # The raw image object passes through unchanged.
    assert result is image_blob


# ---------------------------------------------------------------------------
# Sanity: empty handle list
# ---------------------------------------------------------------------------


async def test_build_tool_registry_returns_empty_registry_for_no_handles() -> None:
    """``build_tool_registry`` doesn't require non-empty handles — but
    note that :class:`McpClientPool` itself refuses an empty config list
    at construction time, so in practice the registry is always
    non-empty. Direct callers of ``build_tool_registry`` (the T4 compat
    shim) might still pass an empty list, and the function should
    gracefully return an empty registry.
    """
    pool = _FakePool([])
    registry = build_tool_registry(pool)  # type: ignore[arg-type]
    assert registry.list_tools() == []
