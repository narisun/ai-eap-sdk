"""Tests for McpClientPool — lifecycle, reconnect, health-check, handles.

The non-extras path doesn't have ``mcp`` installed, so we can't spawn real
subprocesses. Instead each test monkeypatches :meth:`McpClientPool._spawn`
to return a synthetic :class:`McpServerHandle` whose ``session`` is a
small in-test stub class. This lets us exercise the pool's lifecycle
(``__aenter__``/``__aexit__``/``reconnect``/``handles``/``session``/
``health_check``) end-to-end without ever importing ``mcp``.

The stub is a plain class with ``async def list_tools / call_tool`` —
same duck-typing pattern used by ``test_mcp_client_session.py``. Each
test that needs to script behaviour does so by setting attributes on
its stub instance rather than threading mocks through fixtures.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from eap_core.mcp.client import (
    McpClientPool,
    McpServerConfig,
    McpServerHandle,
)
from eap_core.mcp.client.session import McpClientSession


class _StubUpstream:
    """Duck-typed stand-in for the upstream ``mcp.ClientSession``.

    ``list_tools`` returns ``SimpleNamespace(tools=[])`` by default;
    tests that need to script a disconnect set ``list_tools_exc``.
    ``call_tool`` is recorded but its return value is irrelevant for
    pool-level tests (those live in ``test_mcp_client_adapter.py``).
    """

    def __init__(self) -> None:
        self.list_tools_exc: Exception | None = None
        self.call_tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self) -> Any:
        if self.list_tools_exc is not None:
            raise self.list_tools_exc
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.call_tool_calls.append((name, arguments))
        return SimpleNamespace(content=[])


def _make_handle(cfg: McpServerConfig, tool_names: list[str] | None = None) -> McpServerHandle:
    """Build a synthetic handle for ``cfg`` with a fresh stub session."""
    upstream = _StubUpstream()
    session = McpClientSession(
        server_name=cfg.name,
        upstream=upstream,
        request_timeout_s=cfg.request_timeout_s,
    )
    return McpServerHandle(
        config=cfg,
        session=session,
        tool_names=list(tool_names) if tool_names is not None else ["tool_a", "tool_b"],
    )


@pytest.fixture
def patched_spawn(monkeypatch: pytest.MonkeyPatch) -> list[McpServerHandle]:
    """Patch :meth:`McpClientPool._spawn` with a stub that returns a fresh
    synthetic handle per call. Returns the list of every handle the stub
    has produced so tests can assert on reconnect's "fresh handle"
    invariant via identity comparison.
    """
    spawned: list[McpServerHandle] = []

    async def _fake_spawn(self: McpClientPool, cfg: McpServerConfig) -> McpServerHandle:
        handle = _make_handle(cfg)
        spawned.append(handle)
        return handle

    monkeypatch.setattr(McpClientPool, "_spawn", _fake_spawn)
    return spawned


# ---------------------------------------------------------------------------
# Construction-time validation
# ---------------------------------------------------------------------------


def test_pool_rejects_empty_config_list() -> None:
    with pytest.raises(ValueError, match="at least one McpServerConfig"):
        McpClientPool([])


def test_pool_rejects_duplicate_server_names() -> None:
    cfgs = [
        McpServerConfig(name="dup", command="x"),
        McpServerConfig(name="dup", command="x"),
    ]
    with pytest.raises(ValueError, match="duplicate names"):
        McpClientPool(cfgs)


def test_pool_rejects_three_way_duplicate_with_sorted_names() -> None:
    """When three or more configs collide, the error message must list the
    duplicates in sorted order so the diagnostic is stable across Python
    dict-iteration orderings."""
    cfgs = [
        McpServerConfig(name="b", command="x"),
        McpServerConfig(name="a", command="x"),
        McpServerConfig(name="a", command="x"),
        McpServerConfig(name="b", command="x"),
    ]
    with pytest.raises(ValueError, match=r"\['a', 'b'\]"):
        McpClientPool(cfgs)


# ---------------------------------------------------------------------------
# Lifecycle + handle iteration
# ---------------------------------------------------------------------------


async def test_aenter_spawns_one_handle_per_config(
    patched_spawn: list[McpServerHandle],
) -> None:
    cfgs = [
        McpServerConfig(name="a", command="x"),
        McpServerConfig(name="b", command="x"),
    ]
    async with McpClientPool(cfgs) as pool:
        assert len(pool.handles()) == 2
    assert len(patched_spawn) == 2


async def test_handles_returns_in_config_order(
    patched_spawn: list[McpServerHandle],
) -> None:
    """Handle order is the pool's contract — the adapter relies on it for
    deterministic tool registration order."""
    cfgs = [
        McpServerConfig(name="zeta", command="x"),
        McpServerConfig(name="alpha", command="x"),
        McpServerConfig(name="middle", command="x"),
    ]
    async with McpClientPool(cfgs) as pool:
        assert [h.config.name for h in pool.handles()] == ["zeta", "alpha", "middle"]


async def test_aexit_clears_handles(patched_spawn: list[McpServerHandle]) -> None:
    """After the pool exits, ``handles()`` returns an empty list — the
    stack has been unwound and no more sessions are reachable."""
    cfgs = [McpServerConfig(name="a", command="x")]
    pool = McpClientPool(cfgs)
    async with pool:
        assert len(pool.handles()) == 1
    assert pool.handles() == []


# ---------------------------------------------------------------------------
# Session lookup
# ---------------------------------------------------------------------------


async def test_session_by_name_returns_correct_handle(
    patched_spawn: list[McpServerHandle],
) -> None:
    cfgs = [
        McpServerConfig(name="a", command="x"),
        McpServerConfig(name="b", command="x"),
    ]
    async with McpClientPool(cfgs) as pool:
        sa = pool.session("a")
        sb = pool.session("b")
        assert sa.name == "a"
        assert sb.name == "b"
        assert sa is not sb


async def test_session_unknown_name_raises_keyerror(
    patched_spawn: list[McpServerHandle],
) -> None:
    cfgs = [McpServerConfig(name="a", command="x")]
    async with McpClientPool(cfgs) as pool:
        with pytest.raises(KeyError):
            pool.session("nonexistent")


# ---------------------------------------------------------------------------
# Reconnect
# ---------------------------------------------------------------------------


async def test_reconnect_replaces_handle_with_fresh_session(
    patched_spawn: list[McpServerHandle],
) -> None:
    """After ``reconnect``, the handle stored under the server name is a
    NEW instance (the old one is discarded). Identity comparison on the
    handle is the load-bearing assertion — the OLD subprocess's teardown
    is deferred to pool exit (v1.2 follow-up), so we deliberately don't
    assert anything about it here.
    """
    cfgs = [McpServerConfig(name="a", command="x")]
    async with McpClientPool(cfgs) as pool:
        before = pool.handles()[0]
        await pool.reconnect("a")
        after = pool.handles()[0]
        assert before is not after
        assert after.config.name == "a"


async def test_reconnect_unknown_server_raises_keyerror(
    patched_spawn: list[McpServerHandle],
) -> None:
    cfgs = [McpServerConfig(name="a", command="x")]
    async with McpClientPool(cfgs) as pool:
        with pytest.raises(KeyError, match="nonexistent"):
            await pool.reconnect("nonexistent")


async def test_reconnect_preserves_handle_position_in_handles_list(
    patched_spawn: list[McpServerHandle],
) -> None:
    """``handles()`` order is the constructor order; reconnecting the
    middle server must not push it to the end of the list."""
    cfgs = [
        McpServerConfig(name="first", command="x"),
        McpServerConfig(name="middle", command="x"),
        McpServerConfig(name="last", command="x"),
    ]
    async with McpClientPool(cfgs) as pool:
        await pool.reconnect("middle")
        assert [h.config.name for h in pool.handles()] == ["first", "middle", "last"]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def test_health_check_reports_all_healthy(
    patched_spawn: list[McpServerHandle],
) -> None:
    cfgs = [
        McpServerConfig(name="a", command="x"),
        McpServerConfig(name="b", command="x"),
    ]
    async with McpClientPool(cfgs) as pool:
        status = await pool.health_check()
        assert status == {"a": True, "b": True}


async def test_health_check_reports_disconnect_as_false(
    patched_spawn: list[McpServerHandle],
) -> None:
    """When one session's ``list_tools`` raises ``McpServerDisconnectedError``
    (the wrapped form of ``BrokenPipeError`` etc.) the health-check entry
    for that server is ``False`` and the other servers' entries are
    unaffected."""
    cfgs = [
        McpServerConfig(name="healthy", command="x"),
        McpServerConfig(name="dead", command="x"),
    ]
    async with McpClientPool(cfgs) as pool:
        # Reach into the dead server's stub upstream and arm a disconnect.
        dead_handle = pool.handles()[1]
        dead_upstream = dead_handle.session._upstream
        dead_upstream.list_tools_exc = BrokenPipeError("stream closed")

        status = await pool.health_check()
        assert status == {"healthy": True, "dead": False}


async def test_health_check_reports_unknown_exception_as_false(
    patched_spawn: list[McpServerHandle],
) -> None:
    """A non-typed exception during ``list_tools`` must still be reported
    as unhealthy — the pool defensively catches ``Exception`` so a flaky
    upstream doesn't abort the whole sweep mid-iteration.

    Note: ``McpClientSession.list_tools`` only wraps the disconnect
    family (BrokenPipe/Connection/EOF). Any other exception propagates
    unwrapped, and the pool's broad ``except Exception`` clause catches
    it and reports ``False``.
    """
    cfgs = [McpServerConfig(name="flaky", command="x")]
    async with McpClientPool(cfgs) as pool:
        upstream = pool.handles()[0].session._upstream
        upstream.list_tools_exc = RuntimeError("server returned a confused response")

        status = await pool.health_check()
        assert status == {"flaky": False}


async def test_health_check_does_not_auto_reconnect(
    patched_spawn: list[McpServerHandle],
) -> None:
    """``health_check`` is observation, not mutation. After it reports a
    server as unhealthy the handle in the pool is still the same handle
    — the caller is expected to call ``reconnect`` explicitly."""
    cfgs = [McpServerConfig(name="dead", command="x")]
    async with McpClientPool(cfgs) as pool:
        before = pool.handles()[0]
        upstream = before.session._upstream
        upstream.list_tools_exc = BrokenPipeError()

        await pool.health_check()
        assert pool.handles()[0] is before
        # Sanity: the patched_spawn list only grew during __aenter__.
        assert len(patched_spawn) == 1


# ---------------------------------------------------------------------------
# Sanity: pool refuses to be entered twice without exiting
# ---------------------------------------------------------------------------


async def test_pool_session_unknown_before_enter_raises_keyerror() -> None:
    """Before ``__aenter__`` runs, ``_handles`` is empty — looking up any
    name raises ``KeyError``. This is defensive: a caller that
    accidentally invokes ``.session()`` on a not-yet-entered pool gets a
    clear error rather than ``None``.
    """
    pool = McpClientPool([McpServerConfig(name="a", command="x")])
    with pytest.raises(KeyError):
        pool.session("a")


async def test_build_tool_registry_delegates_to_adapter(
    patched_spawn: list[McpServerHandle],
) -> None:
    """``pool.build_tool_registry()`` is a thin delegation to
    ``adapter.build_tool_registry`` — the real adapter logic is exercised
    in ``test_mcp_client_adapter.py``. Here we just verify the delegation
    works end-to-end (a non-empty registry comes back, with the expected
    namespaced names).
    """
    cfgs = [McpServerConfig(name="srv", command="x")]
    async with McpClientPool(cfgs) as pool:
        registry = pool.build_tool_registry()
        tool_names = {spec.name for spec in registry.list_tools()}
        assert tool_names == {"srv__tool_a", "srv__tool_b"}
