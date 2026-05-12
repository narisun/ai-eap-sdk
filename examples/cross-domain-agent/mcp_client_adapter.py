"""v1.0 → v1.1 compatibility shim for the cross-domain-agent example.

Pre-v1.1 this module was a ~149-line per-agent shim implementing
``connect_servers`` / ``build_tool_specs`` / ``ServerHandle`` against
the upstream ``mcp.client.stdio`` API. v1.1 promoted that pattern into
the SDK as :mod:`eap_core.mcp.client` (see the parent package's
``__init__``); this file is now a ~25-line **migration reference** that
delegates to the SDK while preserving the v1.0 public signatures.

For new code, import directly from the SDK::

    from eap_core.mcp.client import McpClientPool, McpServerConfig

    async with McpClientPool([cfg_a, cfg_b]) as pool:
        registry = pool.build_tool_registry()
        await registry.invoke("server-a__list_tables", {})

The headline ``agent.py`` next to this file uses the SDK API directly.
This shim exists so any external caller that imported the v1.0 public
names continues to work after upgrading — the SDK is strictly additive.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from eap_core.mcp.client import McpClientPool, McpServerConfig, McpServerHandle
from eap_core.mcp.client.adapter import build_tool_registry
from eap_core.mcp.types import ToolSpec

# v1.0 public alias. ``McpServerHandle`` is the SDK's dataclass; the
# v1.0 example's ``ServerHandle`` had the same logical role (name +
# session + tool_names) so aliasing is safe. New code should use
# ``McpServerHandle`` directly.
ServerHandle = McpServerHandle


async def connect_servers(
    server_configs: list[dict[str, Any]],
    stack: AsyncExitStack,
) -> list[ServerHandle]:
    """v1.0 entry point — accepts the legacy ``list[dict]`` shape and a
    caller-owned :class:`AsyncExitStack`.

    Internally constructs :class:`McpServerConfig` per server, enters an
    :class:`McpClientPool` on the caller's stack, and returns the pool's
    handles. The pool's teardown is bound to the stack, so when the
    caller exits the stack every subprocess shuts down cleanly — the
    same lifecycle the v1.0 shim provided.
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
    """v1.0 entry point — builds ToolSpec forwarders from a list of
    handles (rather than a pool, which is what the SDK's adapter takes).

    The SDK adapter is pool-shaped because the forwarder needs to call
    ``pool.session(name)`` / ``pool.reconnect(name)`` at invocation
    time. To preserve the v1.0 signature we construct a minimal
    pool-like adapter from the loose handle list — duck-typed with the
    three methods the adapter touches. This is exactly the seam future
    callers migrate ACROSS; it exists here so the v1.0 contract holds.
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
            # The v1.0 shim never had a reconnect concept — surfacing a
            # disconnect was the caller's problem. We preserve that
            # behaviour: tell the user this branch isn't reachable from
            # the v1.0 surface and let them upgrade to McpClientPool.
            raise RuntimeError(
                f"reconnect not supported via the v1.0 compat shim; "
                f"use eap_core.mcp.client.McpClientPool directly to "
                f"reconnect server {name!r}"
            )

    registry = build_tool_registry(_LooseHandlesPool(handles))
    return list(registry.list_tools())
