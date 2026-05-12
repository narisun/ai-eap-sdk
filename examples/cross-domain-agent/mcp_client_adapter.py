"""Backward-compat shim for the cross-domain-agent example.

Re-exports ``connect_servers`` / ``build_tool_specs`` / ``ServerHandle``
entry points for callers that haven't migrated yet. For new code,
import from :mod:`eap_core.mcp.client` directly::

    from eap_core.mcp.client import McpClientPool, McpServerConfig

    async with McpClientPool([cfg_a, cfg_b]) as pool:
        registry = pool.build_tool_registry()
        await registry.invoke("server-a__list_tables", {})

The headline ``agent.py`` next to this file uses the SDK API directly.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from eap_core.mcp.client import McpClientPool, McpServerConfig, McpServerHandle
from eap_core.mcp.client.adapter import build_tool_registry
from eap_core.mcp.types import ToolSpec

# Public alias. ``McpServerHandle`` is the SDK's dataclass with the
# same logical role (name + session + tool_names). New code should
# use ``McpServerHandle`` directly.
ServerHandle = McpServerHandle


async def connect_servers(
    server_configs: list[dict[str, Any]],
    stack: AsyncExitStack,
) -> list[ServerHandle]:
    """Accept the legacy ``list[dict]`` server-config shape and a
    caller-owned :class:`AsyncExitStack`.

    Internally constructs :class:`McpServerConfig` per server, enters an
    :class:`McpClientPool` on the caller's stack, and returns the pool's
    handles. The pool's teardown is bound to the stack, so when the
    caller exits the stack every subprocess shuts down cleanly.

    L2 (v1.2): an empty ``server_configs`` list returns ``[]`` rather
    than constructing a pool. The v1.0 shim signature accepted "no
    servers configured" as a valid input and returned no handles;
    v1.1's :class:`McpClientPool` rejects an empty config list with
    ``ValueError``. Short-circuiting here preserves the v1.0 contract
    so a caller that has dynamic config (and may legitimately have no
    servers in some environments) doesn't crash.
    """
    if not server_configs:
        return []
    cfgs = [
        McpServerConfig(
            name=d["name"],
            command=d["command"],
            args=d.get("args", []),
            cwd=Path(d["cwd"]) if d.get("cwd") else None,
            env=d.get("env"),
        )
        for d in server_configs
    ]
    pool = await stack.enter_async_context(McpClientPool(cfgs))
    return pool.handles()


def build_tool_specs(handles: list[ServerHandle]) -> list[ToolSpec]:
    """Build ToolSpec forwarders from a list of handles (rather than a
    pool, which is what the SDK's adapter takes).

    The SDK adapter is pool-shaped because the forwarder needs to call
    ``pool.session(name)`` / ``pool.reconnect(name)`` at invocation
    time. To preserve the loose-handle signature we construct a minimal
    pool-like adapter from the handle list — duck-typed with the three
    methods the adapter touches. New code should pass an
    :class:`McpClientPool` to ``build_tool_registry`` directly.
    """

    class _LooseHandlesPool:
        def __init__(self, handles_: list[ServerHandle]) -> None:
            self._by_name = {h.config.name: h for h in handles_}
            self._order = [h.config.name for h in handles_]

        def handles(self) -> list[ServerHandle]:
            return [self._by_name[n] for n in self._order]

        def session(self, name: str) -> Any:
            return self._by_name[name].session

        async def reconnect(self, name: str) -> None:
            """v1.0 compat shim: no-op reconnect.

            L4 (v1.2): the v1.0 entry points (``connect_servers`` +
            ``build_tool_specs``) were callable without a pool object,
            so there was no place to put reconnect logic. The SDK's
            adapter forwarder wraps every ``call_tool`` in
            ``try: ... except McpServerDisconnectedError: await
            pool.reconnect(...); raise``; if we raised ``RuntimeError``
            here that ``raise`` would never run and the caller would
            see ``RuntimeError`` instead of the original
            :class:`McpServerDisconnectedError`.

            By making this a no-op the forwarder's recovery path
            completes cleanly and the original disconnect error
            re-raises to the caller — same shape v1.0 callers saw,
            since v1.0 had no reconnect concept at all.
            """
            return None

    registry = build_tool_registry(_LooseHandlesPool(handles))
    return list(registry.list_tools())
