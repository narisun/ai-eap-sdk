"""Typed errors raised by the MCP client subpackage.

All errors derive from ``McpClientError``. Callers can catch the base
to handle any client-side failure or the specific subclass for
fine-grained recovery (e.g. ``McpServerDisconnectedError`` to trigger
reconnect logic, ``McpToolTimeoutError`` to retry the call elsewhere).
"""

from __future__ import annotations


class McpClientError(Exception):
    """Base for all client-side MCP errors. Matches the shape of
    ``eap_core.mcp.types.MCPError`` (server-side base) so a downstream
    handler can ``except (MCPError, McpClientError)`` symmetrically.
    """


class McpServerSpawnError(McpClientError):
    """Raised when the server subprocess fails to start (binary not
    found, non-zero exit during init, stdio handshake failure).
    """


class McpServerDisconnectedError(McpClientError):
    """Raised when the server subprocess closes unexpectedly mid-session.
    The pool catches this and may reconnect depending on configuration;
    direct callers can catch it to retry against a fresh session.
    """


class McpToolTimeoutError(McpClientError):
    """Raised when a ``call_tool`` exceeds its configured timeout.
    Carries the tool name and the timeout value in seconds for log
    diagnostics.
    """

    def __init__(self, tool: str, timeout_s: float) -> None:
        super().__init__(f"MCP tool {tool!r} did not respond within {timeout_s}s")
        self.tool = tool
        self.timeout_s = timeout_s


class McpToolInvocationError(McpClientError):
    """Raised when a remote ``call_tool`` returns a non-OK response or
    the response shape is unrecognised.
    """


class McpOutputSchemaError(McpClientError):
    """Raised in strict-validation mode when a remote tool's response
    fails its advertised ``outputSchema``. Carries both the offending
    payload and the expected schema for diagnostics.
    """

    def __init__(self, tool: str, payload: object, schema: object, reason: str) -> None:
        super().__init__(f"MCP tool {tool!r} response failed outputSchema: {reason}")
        self.tool = tool
        self.payload = payload
        self.schema = schema
        self.reason = reason
