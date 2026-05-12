import json
from datetime import datetime
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


class _Timestamped(BaseModel):
    """Has a datetime field — exercises the difference between
    ``model_dump()`` (returns native ``datetime``) and
    ``model_dump(mode="json")`` (returns ISO 8601 string)."""

    label: str
    when: datetime


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
    """``_json_default`` falls through to ``str()`` for unusual values
    (Path, datetime, UUID). The MCP client gets a stringified
    representation rather than a serialization error."""
    out = _serialize_for_text_content({"path": Path("/tmp/x")})
    parsed = json.loads(out)
    assert parsed == {"path": "/tmp/x"}


def test_serialize_dict_with_nested_basemodel_produces_json_object():
    """Regression for the v0.7.1 gap: a BaseModel nested inside a dict
    was being flattened to its Python repr string via the old
    ``default=str`` fallback. v0.7.2 routes nested BaseModels through
    ``model_dump(mode='json')`` so they become proper JSON objects.
    """
    out = _serialize_for_text_content({"item": _Sample(name="alice", count=5)})
    parsed = json.loads(out)
    assert parsed == {"item": {"name": "alice", "count": 5}}
    # The nested value must be a parsed dict, not a string repr.
    assert isinstance(parsed["item"], dict)


def test_serialize_list_of_basemodels_produces_json_array_of_objects():
    """Same regression as the dict case, but for list-of-BaseModel."""
    out = _serialize_for_text_content(
        [_Sample(name="alice", count=5), _Sample(name="bob", count=3)]
    )
    parsed = json.loads(out)
    assert parsed == [
        {"name": "alice", "count": 5},
        {"name": "bob", "count": 3},
    ]
    assert all(isinstance(item, dict) for item in parsed)


def test_serialize_deeply_nested_basemodel_in_dict_in_list():
    """Recursion: BaseModel inside dict inside list still produces a
    parseable JSON object at every level."""
    out = _serialize_for_text_content(
        [
            {"results": [_Sample(name="a", count=1)]},
            {"results": [_Sample(name="b", count=2), _Sample(name="c", count=3)]},
        ]
    )
    parsed = json.loads(out)
    assert parsed[0]["results"][0] == {"name": "a", "count": 1}
    assert parsed[1]["results"][1]["count"] == 3


def test_serialize_pydantic_v1_basemodel_returns_json():
    """Pydantic v2 ships ``pydantic.v1`` as a compat shim; tools that
    still inherit from ``pydantic.v1.BaseModel`` must serialize to
    JSON too. Without the v1 isinstance branch they'd fall through to
    ``str()`` and emit the v1 ``repr`` — the same class of bug v0.7.1
    closed for v2 models."""
    pytest.importorskip("pydantic.v1")
    from pydantic.v1 import BaseModel as V1Base

    class V1Sample(V1Base):
        name: str
        count: int

    out = _serialize_for_text_content(V1Sample(name="alice", count=5))
    parsed = json.loads(out)
    assert parsed == {"name": "alice", "count": 5}


def test_serialize_pydantic_v1_basemodel_nested_in_dict_returns_json():
    """The v1 branch in ``_json_default`` mirrors the v2 branch: v1
    BaseModels nested inside dict/list returns also become proper
    JSON objects, not repr strings."""
    pytest.importorskip("pydantic.v1")
    from pydantic.v1 import BaseModel as V1Base

    class V1Sample(V1Base):
        name: str
        count: int

    out = _serialize_for_text_content({"item": V1Sample(name="alice", count=5)})
    parsed = json.loads(out)
    assert parsed == {"item": {"name": "alice", "count": 5}}


def test_serialize_nested_basemodel_with_datetime_field_uses_iso_8601():
    """Mutation-pin for the v0.7.2 H1 fix: locks the ``mode="json"``
    requirement on ``model_dump``. Without ``mode="json"``,
    ``model_dump()`` returns a native ``datetime`` that ``json.dumps``
    can't serialize natively — falls through to ``str()`` and emits
    ``"2026-05-11 12:00:00"`` (space-separator) instead of ISO 8601
    ``"2026-05-11T12:00:00"``. External MCP clients parsing the result
    as RFC 3339 timestamps would fail.

    Original v0.7.2 tests used only string/int fields where
    ``model_dump()`` and ``model_dump(mode="json")`` produce identical
    output — a future regression dropping ``mode="json"`` would have
    been undetected. This test catches it.
    """
    out = _serialize_for_text_content(
        {"event": _Timestamped(label="release", when=datetime(2026, 5, 11, 12, 0, 0))}
    )
    parsed = json.loads(out)
    assert parsed == {"event": {"label": "release", "when": "2026-05-11T12:00:00"}}
    # The datetime must be ISO 8601 ("T" separator), not the Python str()
    # default (space separator). This is the mutation-test pin.
    assert "T" in parsed["event"]["when"]
    assert " " not in parsed["event"]["when"]


def test_serialize_v1_basemodel_with_datetime_field_nested_uses_iso_8601():
    """v0.7.3 M-2 fix: v1 BaseModel nested in dict/list serializes
    datetime via ``json.loads(o.json())`` (ISO 8601), not ``o.dict()``
    (which returned native datetime → str() → space-separator format).
    Locks the v1/nested path to the same wire format as the v1/top-level
    path and the v2 paths.
    """
    pytest.importorskip("pydantic.v1")
    from pydantic.v1 import BaseModel as V1Base

    class V1Timestamped(V1Base):
        label: str
        when: datetime

    out = _serialize_for_text_content(
        {"event": V1Timestamped(label="release", when=datetime(2026, 5, 11, 12, 0, 0))}
    )
    parsed = json.loads(out)
    when = parsed["event"]["when"]
    # ISO 8601 — "T" separator, no space.
    assert "T" in when
    assert " " not in when
    assert parsed["event"]["label"] == "release"
