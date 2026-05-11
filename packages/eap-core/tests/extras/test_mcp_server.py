import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytestmark = pytest.mark.extras

from pydantic import BaseModel

from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.server import _serialize_for_text_content, build_mcp_server


class _Sample(BaseModel):
    name: str
    count: int


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


def test_serialize_basemodel_returns_json():
    """Regression: prior versions used ``str(result)`` which emitted the
    pydantic repr (``name='x' count=1``) for BaseModel instances —
    unparseable by non-Python MCP clients. v0.7.1 routes BaseModels
    through ``model_dump_json``.
    """
    out = _serialize_for_text_content(_Sample(name="alice", count=5))
    parsed = json.loads(out)
    assert parsed == {"name": "alice", "count": 5}


def test_serialize_dict_returns_json():
    out = _serialize_for_text_content({"key": "value", "n": 42})
    parsed = json.loads(out)
    assert parsed == {"key": "value", "n": 42}


def test_serialize_list_returns_json():
    out = _serialize_for_text_content([1, 2, 3])
    parsed = json.loads(out)
    assert parsed == [1, 2, 3]


def test_serialize_string_returns_unchanged():
    """Plain strings stay raw — preserves backward compat for tools
    that return text. JSON-encoding would have added quotes."""
    assert _serialize_for_text_content("hello world") == "hello world"


def test_serialize_int_returns_str():
    assert _serialize_for_text_content(42) == "42"


def test_serialize_none_returns_string_none():
    assert _serialize_for_text_content(None) == "None"


def test_serialize_dict_with_non_json_value_falls_back_to_str():
    """``default=str`` keeps json.dumps from raising on unusual values
    (Path, datetime, UUID). The MCP client gets a stringified
    representation rather than a serialization error."""
    out = _serialize_for_text_content({"path": Path("/tmp/x")})
    parsed = json.loads(out)
    assert parsed == {"path": "/tmp/x"}
