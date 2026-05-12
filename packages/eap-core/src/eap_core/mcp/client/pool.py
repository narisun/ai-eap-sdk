"""McpClientPool — lifecycle manager for one or more MCP server subprocesses.

The pool is an async context manager. Entering it spawns every configured
server, opens an ``mcp.ClientSession`` to each over stdio, and returns once
every session has been initialised. Exiting it tears every session and
subprocess down cleanly through the single ``AsyncExitStack`` it owns.

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
flagged for v1.2, which will introduce a per-handle ``AsyncExitStack``
nested inside the pool's outer stack so each handle can be unwound
independently.
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
        """Spawn one server subprocess and open an MCP stdio session against it.

        The ``stdio_client`` and ``ClientSession`` contexts are entered on
        the pool's :class:`AsyncExitStack` so they get torn down when the
        pool exits. The upstream imports are lazy so the non-extras test
        path (which never enters the pool) doesn't need ``mcp`` installed.
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            raise McpServerSpawnError(
                "MCP client requires the [mcp] extra: pip install eap-core[mcp]"
            ) from e

        assert self._stack is not None, "pool not entered"
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            env=cfg.env,
        )
        try:
            read, write = await self._stack.enter_async_context(stdio_client(params))
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

        **v1.2 follow-up.** The :class:`AsyncExitStack` model does not
        support partial unwind of an intermediate context, so this method
        spawns a NEW session/subprocess and replaces the handle. The OLD
        subprocess is torn down only when the pool exits. For long-lived
        agents that call ``reconnect`` many times this leaks file
        descriptors and child processes until pool exit. The fix
        (per-handle nested ``AsyncExitStack``) is deferred to v1.2.

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
