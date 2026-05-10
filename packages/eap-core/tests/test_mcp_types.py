import pytest
from pydantic import ValidationError

from eap_core.mcp.types import MCPError, ToolSpec


def test_tool_spec_minimal():
    spec = ToolSpec(
        name="get_balance", description="...", input_schema={}, output_schema=None, fn=lambda: None
    )
    assert spec.name == "get_balance"
    assert spec.requires_auth is False


def test_tool_spec_rejects_empty_name():
    with pytest.raises(ValidationError):
        ToolSpec(name="", description="x", input_schema={}, output_schema=None, fn=lambda: None)


def test_mcp_error_carries_tool_name():
    e = MCPError(tool_name="x", message="boom")
    assert e.tool_name == "x"
    assert "boom" in str(e)
