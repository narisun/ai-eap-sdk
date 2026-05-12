"""MCP stdio server — wraps McpToolRegistry as an MCP server."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from eap_core.mcp.registry import McpToolRegistry

# Pydantic v2 ships its v1 compat namespace as ``pydantic.v1``. Tools
# that still import ``from pydantic.v1 import BaseModel`` produce
# instances that don't subclass v2's ``BaseModel`` — we need a second
# isinstance check to route them through the v1 ``.json()``/``.dict()``
# API rather than falling through to ``str()``.
try:
    from pydantic.v1 import BaseModel as _V1BaseModel
except ImportError:  # pragma: no cover - v1 compat is always present in v2
    _V1BaseModel = None  # type: ignore[assignment,misc]


def _json_default(o: Any) -> Any:
    """``json.dumps`` fallback that unpacks BaseModel values nested
    inside ``dict``/``list`` returns. Without this, a tool returning
    ``{"item": Inner(a=1)}`` would silently flatten the nested model
    to its Python repr string — the exact bug v0.7.1 closed at the
    top level but missed at one level of depth.

    For v1 BaseModels we round-trip through ``json.loads(o.json())``
    instead of ``o.dict()``. v1's ``.dict()`` returns raw Python types
    (``datetime``, ``UUID``, ``Decimal``) which then fall through to
    the ``str()`` branch below — producing the wrong format on the
    wire (e.g. ``"2026-05-11 12:00:00"`` instead of ISO 8601
    ``"2026-05-11T12:00:00"``). Round-tripping through v1's own
    JSON-mode serialization keeps nested v1 BaseModels consistent
    with the top-level path in ``_serialize_for_text_content``.
    """
    if isinstance(o, BaseModel):
        return o.model_dump(mode="json")
    if _V1BaseModel is not None and isinstance(o, _V1BaseModel):
        return json.loads(o.json())
    return str(o)


def _serialize_for_text_content(result: Any) -> str:
    """Serialize a tool result for embedding in MCP TextContent.text.

    Routes pydantic ``BaseModel`` through ``model_dump_json`` and
    ``dict``/``list`` through ``json.dumps`` (with a recursive default
    handler that unpacks nested BaseModels) so external MCP clients
    receive parseable JSON at every level of nesting. Primitives
    (str/int/bool/None) preserve the original ``str()`` behavior —
    backward-compatible for tools that return raw text.

    History:
    - Prior to v0.7.1 every result went through ``str()``; for
      ``BaseModel`` returns that emitted a Python repr instead of JSON.
    - v0.7.1 fixed the top-level cases but used ``default=str`` for
      nested values, which re-introduced the bug for ``BaseModel``
      nested inside ``dict``/``list``.
    - v0.7.2 (this version) handles nested BaseModels via
      ``_json_default``, and adds pydantic-v1 BaseModel support so
      tools using the compat shim serialize correctly too.
    """
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if _V1BaseModel is not None and isinstance(result, _V1BaseModel):
        return result.json()
    if isinstance(result, (dict, list)):
        return json.dumps(result, default=_json_default)
    return str(result)


def build_mcp_server(registry: McpToolRegistry, *, server_name: str = "eap-core") -> Any:
    """Build an MCP server that exposes all tools in `registry` over stdio."""
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError as e:
        raise ImportError(
            "MCP stdio server requires the [mcp] extra: pip install eap-core[mcp]"
        ) from e

    server: Any = Server(server_name)

    @server.list_tools()  # type: ignore[untyped-decorator,unused-ignore]
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for spec in registry.list_tools()
        ]

    @server.call_tool()  # type: ignore[untyped-decorator,unused-ignore]
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = await registry.invoke(name, arguments)
        return [TextContent(type="text", text=_serialize_for_text_content(result))]

    return server


async def run_stdio(registry: McpToolRegistry, *, server_name: str = "eap-core") -> None:
    """Convenience entry point: build the server and run it over stdio."""
    try:
        from mcp.server.stdio import stdio_server
    except ImportError as e:
        raise ImportError("MCP stdio runner requires the [mcp] extra") from e
    server = build_mcp_server(registry, server_name=server_name)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
