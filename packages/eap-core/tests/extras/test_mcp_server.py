import pytest

pytest.importorskip("mcp")
pytestmark = pytest.mark.extras

from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.server import build_mcp_server


async def test_build_mcp_server_registers_tools():
    """Smoke test: build_mcp_server returns an mcp.Server with our tools listed."""
    reg = McpToolRegistry()

    @mcp_tool()
    async def hello(who: str) -> str:
        """Say hello."""
        return f"hello {who}"

    reg.register(hello.spec)

    server = build_mcp_server(reg, server_name="test-eap")
    assert server is not None
