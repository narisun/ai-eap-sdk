from eap_core.mcp.decorator import mcp_tool

# `default_registry` is intentionally re-imported here for backward
# compatibility (so `from eap_core.mcp import default_registry` keeps
# working) but is NOT advertised in `__all__` — see C10 (v0.5.0) and
# M-N3 (v0.5.0 review). Static-analysis tools walking `__all__` now
# correctly see no public re-export. The redundant `as default_registry`
# alias keeps ruff F401 quiet on the deliberate re-export.
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.registry import default_registry as default_registry
from eap_core.mcp.types import MCPError, ToolSpec

__all__ = [
    "MCPError",
    "McpToolRegistry",
    "ToolSpec",
    "mcp_tool",
]
