"""Tests for the McpClientError hierarchy."""

from __future__ import annotations

from eap_core.mcp.client import (
    McpClientError,
    McpOutputSchemaError,
    McpServerDisconnectedError,
    McpServerSpawnError,
    McpToolInvocationError,
    McpToolTimeoutError,
)
from eap_core.mcp.types import MCPError


def test_all_client_errors_subclass_base():
    for cls in (
        McpServerSpawnError,
        McpServerDisconnectedError,
        McpToolTimeoutError,
        McpToolInvocationError,
        McpOutputSchemaError,
    ):
        assert issubclass(cls, McpClientError)


def test_base_is_not_a_subclass_of_server_side_mcperror():
    """The two hierarchies are sibling, not nested. A downstream handler
    that wants to catch both has to write `except (MCPError, McpClientError)`."""
    assert not issubclass(McpClientError, MCPError)
    assert not issubclass(MCPError, McpClientError)


def test_tool_timeout_error_carries_tool_and_timeout():
    err = McpToolTimeoutError(tool="list_tables", timeout_s=2.5)
    assert err.tool == "list_tables"
    assert err.timeout_s == 2.5
    assert "list_tables" in str(err)
    assert "2.5s" in str(err)


def test_output_schema_error_carries_full_diagnostic():
    err = McpOutputSchemaError(
        tool="query_sql",
        payload={"unexpected": "shape"},
        schema={"type": "object", "required": ["columns", "rows"]},
        reason="missing required key 'columns'",
    )
    assert err.tool == "query_sql"
    assert err.payload == {"unexpected": "shape"}
    assert err.reason == "missing required key 'columns'"
    assert "query_sql" in str(err)
