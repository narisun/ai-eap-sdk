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
    """
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
            # Reconnect requires the full pool lifecycle — not
            # reachable through this loose-handle compat surface. Use
            # ``McpClientPool`` directly when reconnect is needed.
            raise RuntimeError(
                f"reconnect not supported via the loose-handle compat shim; "
                f"use eap_core.mcp.client.McpClientPool directly to "
                f"reconnect server {name!r}"
            )

    registry = build_tool_registry(_LooseHandlesPool(handles))
    return list(registry.list_tools())
