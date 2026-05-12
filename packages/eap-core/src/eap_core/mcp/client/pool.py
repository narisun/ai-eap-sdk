"""McpClientPool — lifecycle manager for one or more MCP servers.

The pool is an async context manager. Entering it spawns every configured
server, opens an ``mcp.ClientSession`` to each, and returns once every
session has been initialised. Exiting it tears every session down cleanly
through the single ``AsyncExitStack`` it owns.

**Two transports.** :class:`McpServerConfig.transport` selects ``"stdio"``
(spawn a subprocess and talk over its stdin/stdout) or ``"http"`` (open a
Streamable-HTTP session to a remote MCP endpoint). The pool's
:meth:`_spawn` method dispatches on the field; the rest of the pool
(lifecycle, reconnect, health-check, iteration) is transport-agnostic
because :class:`McpClientSession` wraps the upstream ``mcp.ClientSession``
via duck typing, and the upstream class is identical across transports.

Per-server state lives in :class:`McpServerHandle`. The pool stores them by
name; :meth:`McpClientPool.session` returns the current handle's
:class:`McpClientSession`.

**Reconnect.** When a session raises :class:`McpServerDisconnectedError`,
calling :meth:`McpClientPool.reconnect` spawns a fresh subprocess + session
and replaces the handle in the pool. The adapter layer
(``eap_core.mcp.client.adapter``) builds tool forwarders that catch
:class:`McpServerDisconnectedError`, invoke ``reconnect`` automatically, and
then re-raise — the caller decides whether to retry.

**Health.** :meth:`McpClientPool.health_check` calls ``list_tools`` on every
session and returns per-server ``True``/``False``. It does not auto-reconnect
— that decision is left to the caller.

**v1.1 implementation note on reconnect leakage (known limitation).** The
``AsyncExitStack`` model does not support partial unwind of one intermediate
context. :meth:`reconnect` therefore spawns a NEW session/subprocess and
replaces the handle; the OLD subprocess is torn down only when the pool
exits (via the stack). For long-lived agents with many reconnects, this
leaks file descriptors and child processes until pool teardown. The leak is
flagged for a future minor (v1.3+), which will introduce a per-handle
``AsyncExitStack`` nested inside the pool's outer stack so each handle can be
unwound independently.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from eap_core.mcp.client.config import McpServerConfig
from eap_core.mcp.client.errors import (
    McpServerDisconnectedError,
    McpServerSpawnError,
)
from eap_core.mcp.client.session import McpClientSession


def _unpack_transport_streams(result: Any, arity: int, cfg_name: str) -> tuple[Any, Any]:
    """Unpack ``(read, write)`` from a transport context-manager's yielded value.

    ``stdio_client(...)`` yields a 2-tuple ``(read, write)``.
    ``sse_client(...)`` (legacy SSE, v1.3) also yields a 2-tuple.
    ``streamable_http_client(...)`` yields a 3-tuple
    ``(read, write, get_session_id)``; v1.2 doesn't use Streamable-HTTP
    session resumption so the third element is discarded.

    Extracted as a pure module-level helper so the arity-dispatch is
    unit-testable without ``mcp`` installed — the integration tests in
    ``tests/extras/test_mcp_client_http_integration.py`` and
    ``tests/extras/test_mcp_client_sse_integration.py`` exercise it
    end-to-end against a real upstream, but the unit tests in
    ``tests/test_mcp_client_pool.py`` catch arity regressions even when
    the integration suite isn't run (or is broken by an upstream
    drift).
    """
    if arity == 2:
        read, write = result
        return read, write
    if arity == 3:
        read, write, _get_session_id = result
        return read, write
    raise McpServerSpawnError(f"unexpected transport arity {arity} for server {cfg_name!r}")


@dataclass
class McpServerHandle:
    """One running server's state inside the pool.

    Created by :meth:`McpClientPool._spawn`; returned in order from
    :meth:`McpClientPool.handles`. Adapter callers receive these to
    iterate per-server tools and build forwarders.

    Attributes:
        config: The original :class:`McpServerConfig` used to spawn this
            server. Preserved so ``reconnect`` can re-spawn with identical
            parameters.
        session: The live :class:`McpClientSession` wrapping the upstream
            ``mcp.ClientSession``.
        tool_names: Names of tools advertised by the remote server, as
            reported by ``tools/list`` at spawn time. Used by the adapter
            to build a ``<server>__<tool>`` namespaced ToolSpec for each.
    """

    config: McpServerConfig
    session: McpClientSession
    tool_names: list[str] = field(default_factory=list)
    tool_output_schemas: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    """Per-tool ``outputSchema`` captured from the remote ``tools/list``
    response at spawn time. Keys are remote tool names (NOT the
    ``<server>__<tool>`` namespaced form). A ``None`` value means the
    remote advertised the tool but did not publish an ``outputSchema``
    — common today, since most MCP servers don't yet emit one. The
    adapter consults this mapping when ``config.validate_output_schemas``
    is True; absent or ``None`` schemas bypass validation entirely.
    """

    @property
    def name(self) -> str:
        """The server's logical name (``self.config.name``). Convenience
        accessor: ``handle.name`` instead of ``handle.config.name``.
        """
        return self.config.name


class McpClientPool:
    """Async context manager for one or more remote MCP servers.

    Typical use::

        async with McpClientPool([cfg_a, cfg_b]) as pool:
            registry = pool.build_tool_registry()
            result = await registry.invoke("server-a__list_tables", {})

    Or, for advanced callers that want raw handles::

        async with McpClientPool([cfg_a, cfg_b]) as pool:
            for handle in pool.handles():
                tools = await handle.session.list_tools()
                ...

    The pool refuses to construct with an empty config list or with
    duplicate server names; both would produce a registry that either
    has nothing to register or registers colliding tool names.
    """

    def __init__(self, configs: list[McpServerConfig]) -> None:
        if not configs:
            raise ValueError("McpClientPool requires at least one McpServerConfig")
        # Detect duplicate names early — the adapter namespaces tools
        # as ``<server-name>__<tool-name>`` and two servers with the
        # same name would silently collide on the second ``register``.
        names = [c.name for c in configs]
        if len(names) != len(set(names)):
            duplicates = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"McpClientPool configs have duplicate names: {duplicates}")
        self._configs: list[McpServerConfig] = list(configs)
        self._stack: AsyncExitStack | None = None
        self._handles: dict[str, McpServerHandle] = {}

    async def __aenter__(self) -> McpClientPool:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for cfg in self._configs:
                self._handles[cfg.name] = await self._spawn(cfg)
        except BaseException:
            # If any spawn fails partway through, unwind the stack so
            # already-spawned subprocesses get torn down cleanly before
            # the exception propagates. Without this the partial pool
            # would leak fds until the interpreter exits.
            await self._stack.__aexit__(None, None, None)
            self._stack = None
            self._handles.clear()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, tb)
            self._stack = None
        self._handles.clear()

    async def _spawn(self, cfg: McpServerConfig) -> McpServerHandle:
        """Dispatch to the transport-specific spawn helper.

        Each helper must return a fully-initialised :class:`McpServerHandle`:
        the upstream ``mcp.ClientSession`` has been entered into the pool's
        :class:`AsyncExitStack` and its ``initialize`` + ``list_tools`` calls
        have completed.

        Pydantic's ``Literal["stdio", "http", "sse", "websocket"]``
        discriminator already rejects unknown transport strings at config
        construction time, so the final ``raise`` is purely defensive — it
        documents the contract a future transport must satisfy (add a new
        ``_spawn_<name>`` helper and route to it here).

        The ``websocket`` branch raises explicitly with a "added in T2"
        marker so the Literal value is recognised even before the
        WebSocket spawn body lands — without the branch the dispatcher
        would fall through to the generic "unsupported transport" error,
        which would be confusing because the Literal accepts the value.
        """
        assert self._stack is not None, "pool not entered"
        if cfg.transport == "stdio":
            return await self._spawn_stdio(cfg)
        if cfg.transport == "http":
            return await self._spawn_http(cfg)
        if cfg.transport == "sse":
            return await self._spawn_sse(cfg)
        if cfg.transport == "websocket":
            # Placeholder: T2 will replace this with a real
            # ``_spawn_websocket`` helper. Until then the Literal still
            # accepts ``"websocket"`` (config validation enforces URL +
            # forbids headers/auth) but spawning is not wired up.
            raise McpServerSpawnError(
                "websocket transport added in T2 — configuration accepted but "
                "spawn helper not yet implemented in this build"
            )
        raise McpServerSpawnError(
            f"unsupported transport {cfg.transport!r} for server {cfg.name!r}"
        )

    async def _spawn_stdio(self, cfg: McpServerConfig) -> McpServerHandle:
        """Spawn a subprocess and open an MCP stdio session against it.

        ``stdio_client(...)`` yields a 2-tuple ``(read, write)``; the shared
        :meth:`_open_session` path handles entering the transport context
        manager on the pool's stack and unpacking that pair.
        """
        try:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert self._stack is not None, "pool not entered"
        assert cfg.command is not None  # enforced by McpServerConfig validator
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            env=cfg.env,
        )
        return await self._open_session(cfg, stdio_client(params), arity=2)

    async def _spawn_http(self, cfg: McpServerConfig) -> McpServerHandle:
        """Open a Streamable-HTTP session against a remote MCP server.

        ``streamable_http_client(...)`` yields a 3-tuple
        ``(read, write, get_session_id)``; v1.2 does not use Streamable-HTTP
        session resumption so the third element is discarded by
        :meth:`_open_session` when ``arity=3``.

        The upstream ``streamable_http_client`` signature is tight — it
        takes ``url`` and a single ``http_client: httpx.AsyncClient | None``.
        Per-request HTTP config (headers, auth) goes onto the AsyncClient
        at construction time. The upstream helper
        :func:`create_mcp_http_client` (imported privately into
        ``mcp.client.streamable_http`` from ``mcp.shared._httpx_utils``)
        applies the MCP-recommended httpx defaults; we apply
        ``cfg.headers`` and ``cfg.auth`` on top and enter the resulting
        client onto the pool's exit stack so teardown is symmetric with
        the stdio path.

        ``cfg.auth`` is typed as ``Any`` to keep ``httpx`` out of the core
        import path; the upstream helper expects ``httpx.Auth | None`` and
        accepts our value as-is. A non-``Auth`` runtime value will fail
        loudly on the first HTTP request — that's the right contract.
        """
        try:
            # ``create_mcp_http_client`` is imported into
            # ``mcp.client.streamable_http`` from
            # ``mcp.shared._httpx_utils`` but isn't in the module's
            # ``__all__``, so strict mypy flags it as not-explicitly-
            # exported. The defensive [attr-defined, unused-ignore]
            # pair handles both states across upstream versions —
            # mirrors the vertex.py pattern (commit 44c0fae) for the
            # google-cloud-aiplatform stub drift.
            from mcp.client.streamable_http import (  # type: ignore[attr-defined, unused-ignore]
                create_mcp_http_client,
            )
            from mcp.client.streamable_http import (
                streamable_http_client as _streamable_http_client,
            )
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert self._stack is not None, "pool not entered"
        assert cfg.url is not None  # enforced by McpServerConfig validator
        # Build an MCP-defaulted httpx client carrying the per-config
        # headers/auth, and enter it onto the pool's exit stack so it
        # closes cleanly on pool teardown.
        http_client = await self._stack.enter_async_context(
            create_mcp_http_client(headers=cfg.headers, auth=cfg.auth)
        )
        transport_cm = _streamable_http_client(cfg.url, http_client=http_client)
        return await self._open_session(cfg, transport_cm, arity=3)

    async def _spawn_sse(self, cfg: McpServerConfig) -> McpServerHandle:
        """Open a legacy SSE session against a remote MCP server.

        Unlike ``streamable_http_client``, ``sse_client`` keeps the
        original keyword API: ``headers``, ``timeout``, ``auth`` are
        passed directly — there is no ``httpx.AsyncClient`` intermediate
        to construct. Returns a 2-tuple ``(read, write)`` (no session-id
        callback), so the shared :meth:`_open_session` path consumes it
        with ``arity=2``, same as stdio.

        The ``timeout=min(cfg.request_timeout_s, 30.0)`` clamp targets
        ``sse_client``'s **connection** timeout (defaults to 5s
        upstream). ``cfg.request_timeout_s`` is the per-call timeout
        used by :class:`McpClientSession.call_tool`; the two are
        different concepts. Clamping at 30s avoids hanging
        indefinitely if a caller raised ``request_timeout_s`` very
        high, while still respecting an aggressively-low per-call
        setting.

        ``cfg.auth`` is typed as ``Any`` to keep ``httpx`` out of the
        core import path; the upstream helper expects
        ``httpx.Auth | None`` and accepts our value as-is.
        """
        try:
            from mcp.client.sse import sse_client
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert self._stack is not None, "pool not entered"
        assert cfg.url is not None  # enforced by McpServerConfig validator
        transport_cm = sse_client(
            cfg.url,
            headers=cfg.headers,
            auth=cfg.auth,
            timeout=min(cfg.request_timeout_s, 30.0),
        )
        return await self._open_session(cfg, transport_cm, arity=2)

    async def _open_session(
        self,
        cfg: McpServerConfig,
        transport_cm: Any,
        *,
        arity: int,
    ) -> McpServerHandle:
        """Shared session-initialisation path for both transports.

        ``transport_cm`` is the async context manager returned by either
        ``stdio_client`` (2-tuple read/write) or ``streamablehttp_client``
        (3-tuple read/write/get_session_id). ``arity`` tells us which shape
        to unpack; the third element (Streamable-HTTP's
        ``get_session_id`` callback) is dropped because v1.2 doesn't use
        session resumption.

        The transport context and the wrapping ``ClientSession`` are both
        entered on the pool's :class:`AsyncExitStack` so teardown is
        symmetric across transports.
        """
        try:
            from mcp import ClientSession
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert self._stack is not None, "pool not entered"
        try:
            result = await self._stack.enter_async_context(transport_cm)
            read, write = _unpack_transport_streams(result, arity, cfg.name)
            upstream = await self._stack.enter_async_context(ClientSession(read, write))
            await upstream.initialize()
            tools_response = await upstream.list_tools()
        except Exception as e:
            raise McpServerSpawnError(f"failed to spawn MCP server {cfg.name!r}: {e}") from e

        session = McpClientSession(
            server_name=cfg.name,
            upstream=upstream,
            request_timeout_s=cfg.request_timeout_s,
        )
        # Capture each tool's advertised ``outputSchema`` (which the
        # upstream ``mcp.types.Tool`` exposes as an optional attribute).
        # Tools that don't publish one map to ``None``; the adapter
        # treats that as "skip validation for this tool" even when the
        # pool's config opts in. Most MCP servers don't yet emit
        # outputSchema, so this map is sparse in practice.
        tool_output_schemas: dict[str, dict[str, Any] | None] = {
            t.name: getattr(t, "outputSchema", None) for t in tools_response.tools
        }
        return McpServerHandle(
            config=cfg,
            session=session,
            tool_names=[t.name for t in tools_response.tools],
            tool_output_schemas=tool_output_schemas,
        )

    def handles(self) -> list[McpServerHandle]:
        """Return the list of currently-live server handles.

        Order matches the order of configs passed to the constructor. A
        server that was reconnected appears in its original position with
        the fresh handle in place of the old one.
        """
        return [self._handles[c.name] for c in self._configs if c.name in self._handles]

    def session(self, server_name: str) -> McpClientSession:
        """Return the current session for ``server_name``.

        Raises:
            KeyError: if ``server_name`` was never in the pool config. This
                is a caller bug — use :meth:`handles` to iterate live
                servers without name lookups.
        """
        return self._handles[server_name].session

    async def reconnect(self, server_name: str) -> None:
        """Tear down and re-spawn one server's subprocess.

        On success the handle in :attr:`_handles` is replaced with a fresh
        :class:`McpServerHandle` whose :class:`McpClientSession` wraps the
        new ``mcp.ClientSession``.

        **Known limitation.** The :class:`AsyncExitStack` model does not
        support partial unwind of an intermediate context, so this method
        spawns a NEW session/subprocess and replaces the handle. The OLD
        subprocess is torn down only when the pool exits. For long-lived
        agents that call ``reconnect`` many times this leaks file
        descriptors and child processes until pool exit. The fix
        (per-handle nested ``AsyncExitStack``) is deferred to a future
        minor (v1.3+).

        Raises:
            KeyError: if ``server_name`` is not in the pool.
        """
        if server_name not in self._handles:
            raise KeyError(f"unknown server: {server_name!r}")
        cfg = self._handles[server_name].config
        self._handles[server_name] = await self._spawn(cfg)

    async def health_check(self) -> dict[str, bool]:
        """Call ``list_tools`` against every live session.

        Returns a dict mapping ``server-name -> True`` (healthy) or
        ``False`` (the call raised). Does not auto-reconnect — the caller
        decides whether to invoke :meth:`reconnect` for any ``False``
        entries.
        """
        results: dict[str, bool] = {}
        for handle in self.handles():
            try:
                await handle.session.list_tools()
            except McpServerDisconnectedError:
                results[handle.config.name] = False
            except Exception:
                # Defensive: a flaky upstream that surfaces a non-typed
                # exception during ``list_tools`` still counts as
                # unhealthy from the pool's point of view. Don't let one
                # bad session abort the rest of the sweep.
                results[handle.config.name] = False
            else:
                results[handle.config.name] = True
        return results

    def build_tool_registry(self) -> Any:
        """Build an :class:`McpToolRegistry` pre-populated with forwarders.

        For every remote tool on every server, registers a namespaced
        ``<server-name>__<tool-name>`` :class:`ToolSpec` whose ``fn``
        forwards through this pool's session for that server.

        Implementation lives in ``eap_core.mcp.client.adapter`` to keep
        ``pool.py`` lifecycle-only. Imported lazily so static analyses
        that walk this module don't have to load the adapter.
        """
        from eap_core.mcp.client.adapter import build_tool_registry

        return build_tool_registry(self)
