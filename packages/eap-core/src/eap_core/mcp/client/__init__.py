"""First-class MCP client adapter for EAP-Core agents.

Replaces the per-agent shim that lived in
``examples/cross-domain-agent/mcp_client_adapter.py`` through v1.0.
The shim's behaviour is preserved end-to-end; this subpackage adds
structured config, typed errors, session timeout, output-schema
validation, observability spans, and a pool with reconnect/health
semantics.

Public surface (the only names callers should import):

    from eap_core.mcp.client import (
        McpServerConfig,
        McpClientPool,
        McpClientError,
        McpServerDisconnectedError,
        McpToolTimeoutError,
        McpOutputSchemaError,
    )

Typical use:

    async with McpClientPool([cfg_a, cfg_b]) as pool:
        registry = pool.build_tool_registry()
        await registry.invoke("server-a__list_tables", {})
"""

from eap_core.mcp.client.config import McpServerConfig
from eap_core.mcp.client.errors import (
    McpClientError,
    McpOutputSchemaError,
    McpServerDisconnectedError,
    McpServerSpawnError,
    McpToolInvocationError,
    McpToolTimeoutError,
)

__all__ = [
    "McpClientError",
    "McpOutputSchemaError",
    "McpServerConfig",
    "McpServerDisconnectedError",
    "McpServerSpawnError",
    "McpToolInvocationError",
    "McpToolTimeoutError",
]

# McpClientPool is added to __all__ in Task 3; left out here so the
# T1 commit lints cleanly without forward-referencing types T3
# introduces.
