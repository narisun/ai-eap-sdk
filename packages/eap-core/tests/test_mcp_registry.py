import pytest

from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry, default_registry
from eap_core.mcp.types import MCPError


@pytest.fixture
def reg():
    return McpToolRegistry()


async def test_register_and_dispatch(reg: McpToolRegistry):
    @mcp_tool()
    async def add(a: int, b: int) -> int:
        return a + b

    reg.register(add.spec)
    result = await reg.invoke("add", {"a": 2, "b": 3})
    assert result == 5


async def test_invoke_unknown_tool_raises(reg: McpToolRegistry):
    with pytest.raises(MCPError, match="not found"):
        await reg.invoke("missing", {})


async def test_invoke_validates_args_against_schema(reg: McpToolRegistry):
    @mcp_tool()
    async def add(a: int, b: int) -> int:
        return a + b

    reg.register(add.spec)
    with pytest.raises(MCPError, match="validation"):
        await reg.invoke("add", {"a": "not-an-int", "b": 3})


def test_list_tools_returns_specs(reg: McpToolRegistry):
    @mcp_tool()
    async def echo(x: str) -> str:
        return x

    reg.register(echo.spec)
    specs = reg.list_tools()
    assert len(specs) == 1
    assert specs[0].name == "echo"


async def test_invoke_supports_sync_function(reg: McpToolRegistry):
    @mcp_tool()
    def doubler(x: int) -> int:
        return x * 2

    reg.register(doubler.spec)
    result = await reg.invoke("doubler", {"x": 5})
    assert result == 10


def test_default_registry_is_singleton():
    a = default_registry()
    b = default_registry()
    assert a is b


async def test_invoke_tool_that_raises_wraps_in_mcp_error(reg: McpToolRegistry):
    @mcp_tool()
    async def broken(x: int) -> int:
        raise ValueError("boom")

    reg.register(broken.spec)
    with pytest.raises(MCPError, match="boom"):
        await reg.invoke("broken", {"x": 1})


async def test_invoke_tool_re_raises_mcp_error(reg: McpToolRegistry):
    @mcp_tool()
    async def passthrough(x: str) -> str:
        raise MCPError(tool_name="passthrough", message="direct mcp error")

    reg.register(passthrough.spec)
    with pytest.raises(MCPError, match="direct mcp error"):
        await reg.invoke("passthrough", {"x": "hi"})


async def test_invoke_no_input_schema_skips_validation(reg: McpToolRegistry):
    """A ToolSpec with empty input_schema should still dispatch successfully."""

    @mcp_tool()
    async def no_params() -> str:
        return "ok"

    reg.register(no_params.spec)
    result = await reg.invoke("no_params", {})
    assert result == "ok"


def test_get_returns_none_for_missing(reg: McpToolRegistry):
    assert reg.get("nonexistent") is None
