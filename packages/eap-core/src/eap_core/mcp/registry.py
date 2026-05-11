"""McpToolRegistry — discovery and dispatch for MCP-decorated tools."""

from __future__ import annotations

import asyncio
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as jsonschema_validate

from eap_core.exceptions import IdentityError
from eap_core.mcp.types import MCPError, ToolSpec


class McpToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def invoke(
        self,
        name: str,
        args: dict[str, Any],
        *,
        identity: Any | None = None,
    ) -> Any:
        """Validate and dispatch a registered tool.

        When the registered ``ToolSpec.requires_auth`` is True the caller
        MUST plumb a non-``None`` ``identity`` (typically
        ``ctx.identity`` from the middleware pipeline). The dispatcher
        refuses with ``IdentityError`` otherwise — the ``requires_auth``
        flag is a load-bearing authorization gate, not a hint.
        """
        spec = self._specs.get(name)
        if spec is None:
            raise MCPError(tool_name=name, message="tool not found in registry")
        if spec.requires_auth and identity is None:
            raise IdentityError(
                f"tool {name!r} has requires_auth=True but no identity was passed to "
                "McpToolRegistry.invoke; the dispatcher refuses unauthenticated calls"
            )
        if spec.input_schema:
            try:
                jsonschema_validate(args, spec.input_schema)
            except JsonSchemaError as e:
                raise MCPError(
                    tool_name=name,
                    message=f"input validation failed: {e.message}",
                ) from e
        try:
            if spec.is_async:
                return await spec.fn(**args)
            return await asyncio.to_thread(spec.fn, **args)
        except MCPError:
            raise
        except Exception as e:
            raise MCPError(tool_name=name, message=f"tool raised: {e}") from e


_DEFAULT: McpToolRegistry | None = None


def default_registry() -> McpToolRegistry:
    """Module-level singleton the @mcp_tool decorator can auto-register into."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = McpToolRegistry()
    return _DEFAULT
