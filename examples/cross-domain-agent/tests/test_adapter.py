"""Adapter unit tests - exercise build_tool_specs against a stub
ClientSession to verify namespace prefixing, closure capture, and
response decoding without spawning real subprocesses.

The closure-capture test (``test_forwarder_invokes_correct_remote_tool_with_kwargs``)
is the load-bearing case. ``build_tool_specs`` loops over tool names
to build per-tool forwarders. If the forwarder is defined inline
inside the loop, the closure captures the LOOP variable - every
forwarder ends up calling the LAST remote tool name. The adapter
must extract a factory function (``_build_one``) so each forwarder
binds its own ``remote_name``. This test reads through every spec
the adapter built and verifies each forwarder routes to its own
tool.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

from mcp_client_adapter import ServerHandle, build_tool_specs  # noqa: E402


class _StubResponse:
    """Minimal mcp.types.CallToolResult lookalike."""

    def __init__(self, *, text: str | None, has_content: bool = True) -> None:
        if not has_content:
            self.content: list = []
        elif text is None:
            self.content = [SimpleNamespace()]  # no .text attr
        else:
            self.content = [SimpleNamespace(text=text)]


@pytest.mark.asyncio
async def test_build_tool_specs_namespaces_each_tool_with_server_name():
    h1 = ServerHandle(
        name="bankdw", session=AsyncMock(), tool_names=["query_sql", "list_tables"]
    )
    h2 = ServerHandle(
        name="sfcrm", session=AsyncMock(), tool_names=["query_sql"]
    )
    specs = build_tool_specs([h1, h2])
    names = sorted(s.name for s in specs)
    assert names == [
        "bankdw__list_tables",
        "bankdw__query_sql",
        "sfcrm__query_sql",
    ]


@pytest.mark.asyncio
async def test_forwarder_invokes_correct_remote_tool_with_kwargs():
    """Closure capture must pin each forwarder to its own remote name,
    not the last name in the loop. If this fails, the adapter inlined
    the forwarder inside the loop - extract ``_build_one`` and pass
    ``remote_name`` as a parameter."""
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_StubResponse(text=json.dumps({"row_count": 7}))
    )
    h = ServerHandle(
        name="bankdw", session=session, tool_names=["query_sql", "list_tables"]
    )
    specs = build_tool_specs([h])

    list_spec = next(s for s in specs if s.name == "bankdw__list_tables")
    await list_spec.fn()  # no kwargs
    session.call_tool.assert_called_with("list_tables", {})

    session.call_tool.reset_mock()
    query_spec = next(s for s in specs if s.name == "bankdw__query_sql")
    result = await query_spec.fn(sql="SELECT 1", limit=10)
    session.call_tool.assert_called_with(
        "query_sql", {"sql": "SELECT 1", "limit": 10}
    )
    assert result == {"row_count": 7}


@pytest.mark.asyncio
async def test_forwarder_decodes_json_response():
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_StubResponse(
            text=json.dumps({"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}]})
        )
    )
    h = ServerHandle(name="x", session=session, tool_names=["t"])
    [spec] = build_tool_specs([h])
    result = await spec.fn()
    assert result == {"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}]}


@pytest.mark.asyncio
async def test_forwarder_returns_raw_text_when_response_is_not_json():
    """Primitive tool returns (str/int/bool) are serialised server-side
    via ``str()`` not JSON. The adapter returns the raw text for these
    rather than raising on the json.JSONDecodeError. Tools that need
    structured returns should return a pydantic ``BaseModel`` or a
    plain ``dict``/``list`` — those land as JSON.
    """
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_StubResponse(text="this is plain text, not json")
    )
    h = ServerHandle(name="x", session=session, tool_names=["t"])
    [spec] = build_tool_specs([h])
    result = await spec.fn()
    assert result == "this is plain text, not json"


@pytest.mark.asyncio
async def test_forwarder_returns_none_when_response_has_empty_content():
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=_StubResponse(text=None, has_content=False)
    )
    h = ServerHandle(name="x", session=session, tool_names=["t"])
    [spec] = build_tool_specs([h])
    assert await spec.fn() is None
