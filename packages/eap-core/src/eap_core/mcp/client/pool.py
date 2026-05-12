"""McpClientPool — lifecycle manager for one or more MCP servers.

The pool is an async context manager. Entering it spawns every configured
server, opens an ``mcp.ClientSession`` to each, and returns once every
session has been initialised. Exiting it tears every session down cleanly
through the single ``AsyncExitStack`` it owns.

**Four transports.** :class:`McpServerConfig.transport` selects one of
``"stdio"`` (spawn a subprocess and talk over its stdin/stdout),
``"http"`` (open a Streamable-HTTP session — the modern MCP HTTP
protocol), ``"sse"`` (open a legacy SSE session — the older HTTP
protocol some servers still expose), or ``"websocket"`` (open an
MCP-over-WebSocket session). The pool's :meth:`_spawn` method
dispatches on the field; the rest of the pool (lifecycle, reconnect,
health-check, iteration) is transport-agnostic because
:class:`McpClientSession` wraps the upstream ``mcp.ClientSession`` via
duck typing, and the upstream class is identical across transports.
WebSocket support is URL-only (upstream ``websocket_client`` takes no
headers/auth parameters — see ``_spawn_websocket`` for the
limitation note); HTTP and SSE accept ``headers`` and ``auth``,
including the ``BearerTokenAuth`` adapter that wraps an EAP-Core
identity (``NonHumanIdentity`` / ``VertexAgentIdentityToken`` / etc.)
as an ``httpx.Auth`` flow.

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

**Per-handle nested stacks (v1.4).** Each handle owns its own
``AsyncExitStack`` which is itself entered onto the pool's outer
stack. The pool's outer stack therefore owns a list of handle
stacks; each handle stack owns the transport context manager and the
wrapped ``mcp.ClientSession`` for that one server. This gives
:meth:`reconnect` a clean partial-unwind: it closes the old handle's
stack (tearing down the subprocess/connection and session in LIFO
order) before spawning the replacement onto the outer stack. Pool
exit still works the same way — the outer stack closes every handle
stack in reverse order of entry.
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

    _stack: AsyncExitStack | None = field(default=None, repr=False)
    """The per-handle nested :class:`AsyncExitStack` that owns this
    handle's transport context manager and wrapped ``mcp.ClientSession``.
    Internal — assigned by :meth:`McpClientPool._spawn` after the
    transport/session have been entered. :meth:`McpClientPool.reconnect`
    closes it to tear down the OLD subprocess/connection cleanly before
    spawning the replacement. ``None`` only for synthetic handles built
    by tests that bypass ``_spawn`` entirely.
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
        """Spawn one server onto a fresh per-handle nested stack.

        The pool's outer ``_stack`` owns a list of per-handle
        :class:`AsyncExitStack` instances; each handle stack in turn
        owns the transport context manager and the wrapped
        ``mcp.ClientSession`` for that one server. Layering the stacks
        this way lets :meth:`reconnect` close just the old handle's
        stack (LIFO unwind of session → transport) without disturbing
        any sibling handles or the outer stack itself.

        Pydantic's ``Literal["stdio", "http", "sse", "websocket"]``
        discriminator already rejects unknown transport strings at
        config construction time, so :meth:`_dispatch_spawn`'s final
        ``raise`` is purely defensive — it documents the contract a
        future transport must satisfy (add a new ``_spawn_<name>``
        helper and route to it here).
        """
        assert self._stack is not None, "pool not entered"
        handle_stack = AsyncExitStack()
        await self._stack.enter_async_context(handle_stack)
        handle = await self._dispatch_spawn(cfg, handle_stack)
        handle._stack = handle_stack
        return handle

    async def _dispatch_spawn(
        self,
        cfg: McpServerConfig,
        handle_stack: AsyncExitStack,
    ) -> McpServerHandle:
        """Dispatch to the transport-specific spawn helper.

        Each helper must return a fully-initialised
        :class:`McpServerHandle`: the transport context manager and the
        upstream ``mcp.ClientSession`` have been entered into the
        provided ``handle_stack`` and the session's ``initialize`` +
        ``list_tools`` calls have completed. The caller
        (:meth:`_spawn`) attaches ``handle_stack`` to the returned
        handle for later teardown.
        """
        if cfg.transport == "stdio":
            return await self._spawn_stdio(cfg, handle_stack)
        if cfg.transport == "http":
            return await self._spawn_http(cfg, handle_stack)
        if cfg.transport == "sse":
            return await self._spawn_sse(cfg, handle_stack)
        if cfg.transport == "websocket":
            return await self._spawn_websocket(cfg, handle_stack)
        raise McpServerSpawnError(
            f"unsupported transport {cfg.transport!r} for server {cfg.name!r}"
        )

    async def _spawn_stdio(
        self,
        cfg: McpServerConfig,
        handle_stack: AsyncExitStack,
    ) -> McpServerHandle:
        """Spawn a subprocess and open an MCP stdio session against it.

        ``stdio_client(...)`` yields a 2-tuple ``(read, write)``; the shared
        :meth:`_open_session` path handles entering the transport context
        manager on the per-handle stack and unpacking that pair.
        """
        try:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert cfg.command is not None  # enforced by McpServerConfig validator
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            env=cfg.env,
        )
        return await self._open_session(
            cfg, stdio_client(params), arity=2, handle_stack=handle_stack
        )

    async def _spawn_http(
        self,
        cfg: McpServerConfig,
        handle_stack: AsyncExitStack,
    ) -> McpServerHandle:
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
        client onto the per-handle stack so teardown is symmetric with
        the stdio path and so :meth:`reconnect` tears the httpx client
        down with the rest of the handle.

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

        assert cfg.url is not None  # enforced by McpServerConfig validator
        # Build an MCP-defaulted httpx client carrying the per-config
        # headers/auth, and enter it onto the per-handle stack so it
        # closes cleanly on either handle teardown (reconnect) or pool
        # teardown.
        http_client = await handle_stack.enter_async_context(
            create_mcp_http_client(headers=cfg.headers, auth=cfg.auth)
        )
        transport_cm = _streamable_http_client(cfg.url, http_client=http_client)
        return await self._open_session(cfg, transport_cm, arity=3, handle_stack=handle_stack)

    async def _spawn_sse(
        self,
        cfg: McpServerConfig,
        handle_stack: AsyncExitStack,
    ) -> McpServerHandle:
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

        assert cfg.url is not None  # enforced by McpServerConfig validator
        transport_cm = sse_client(
            cfg.url,
            headers=cfg.headers,
            auth=cfg.auth,
            timeout=min(cfg.request_timeout_s, 30.0),
        )
        return await self._open_session(cfg, transport_cm, arity=2, handle_stack=handle_stack)

    async def _spawn_websocket(
        self,
        cfg: McpServerConfig,
        handle_stack: AsyncExitStack,
    ) -> McpServerHandle:
        """Open an MCP-over-WebSocket session against a remote MCP server.

        Upstream ``websocket_client`` is URL-only — it takes no
        ``headers`` or ``auth`` parameters. WebSocket MCP servers
        requiring authentication must encode credentials in the URL
        (query string or path segment) until upstream gains
        parameters. The v1.3 config validator forbids
        ``headers``/``auth`` for ``transport="websocket"`` configs so the
        limitation surfaces loudly at config construction time rather
        than silently dropping the values. When upstream extends
        ``websocket_client``'s signature, this method will be extended
        in lockstep and the validator relaxed.

        Returns a 2-tuple ``(read, write)`` — no session-id callback —
        so the shared :meth:`_open_session` path consumes it with
        ``arity=2``, same as stdio and SSE.
        """
        try:
            from mcp.client.websocket import websocket_client
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert cfg.url is not None  # enforced by McpServerConfig validator
        transport_cm = websocket_client(cfg.url)
        return await self._open_session(cfg, transport_cm, arity=2, handle_stack=handle_stack)

    async def _open_session(
        self,
        cfg: McpServerConfig,
        transport_cm: Any,
        *,
        arity: int,
        handle_stack: AsyncExitStack,
    ) -> McpServerHandle:
        """Shared session-initialisation path for all transports.

        ``transport_cm`` is the async context manager returned by either
        ``stdio_client`` (2-tuple read/write), ``sse_client`` /
        ``websocket_client`` (also 2-tuple), or ``streamablehttp_client``
        (3-tuple read/write/get_session_id). ``arity`` tells us which
        shape to unpack; the third element (Streamable-HTTP's
        ``get_session_id`` callback) is dropped because v1.2 doesn't
        use session resumption.

        The transport context and the wrapping ``ClientSession`` are
        both entered on the per-handle ``handle_stack`` (not the
        pool's outer stack) so :meth:`reconnect` can unwind them
        independently of every other handle in the pool.
        """
        try:
            from mcp import ClientSession
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        try:
            result = await handle_stack.enter_async_context(transport_cm)
            read, write = _unpack_transport_streams(result, arity, cfg.name)
            upstream = await handle_stack.enter_async_context(ClientSession(read, write))
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
        """Tear down and re-spawn one server's subprocess/connection.

        Closes the old handle's per-handle :class:`AsyncExitStack`
        (LIFO unwind: wrapped ``mcp.ClientSession`` first, then the
        transport context manager — for HTTP this includes the
        per-handle httpx client) before spawning the replacement onto
        a fresh per-handle stack. On success the handle in
        :attr:`_handles` is replaced with a fresh
        :class:`McpServerHandle` whose :class:`McpClientSession` wraps
        the new ``mcp.ClientSession``.

        Raises:
            KeyError: if ``server_name`` is not in the pool.
        """
        if server_name not in self._handles:
            raise KeyError(f"unknown server: {server_name!r}")
        old_handle = self._handles[server_name]
        cfg = old_handle.config
        if old_handle._stack is not None:
            # Unwind just this handle's resources. The pool's outer
            # stack still has a reference to the (now-closed) handle
            # stack; closing an already-closed AsyncExitStack on pool
            # exit is a documented no-op, so the bookkeeping stays
            # honest.
            await old_handle._stack.aclose()
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
