from pydantic import BaseModel

from eap_core.mcp.decorator import mcp_tool


def test_decorator_extracts_name_from_function_default():
    @mcp_tool()
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        return user_id

    assert lookup_user.spec.name == "lookup_user"
    assert "Look up a user" in lookup_user.spec.description


def test_decorator_overrides_name_and_description():
    @mcp_tool(name="get_user", description="custom desc")
    async def lookup_user(user_id: str) -> str:
        return user_id

    assert lookup_user.spec.name == "get_user"
    assert lookup_user.spec.description == "custom desc"


def test_decorator_generates_input_schema_from_primitives():
    @mcp_tool()
    async def add(a: int, b: int = 0) -> int:
        """Sum two ints."""
        return a + b

    schema = add.spec.input_schema
    assert schema["type"] == "object"
    assert "a" in schema["properties"]
    assert "b" in schema["properties"]
    assert schema["properties"]["a"]["type"] == "integer"
    assert "a" in schema["required"]
    assert "b" not in schema.get("required", [])


def test_decorator_generates_input_schema_from_pydantic_model():
    class Query(BaseModel):
        text: str
        limit: int = 10

    @mcp_tool()
    async def search(q: Query) -> list[str]:
        return []

    schema = search.spec.input_schema
    assert schema["type"] == "object"
    assert "q" in schema["properties"]


def test_decorator_marks_requires_auth():
    @mcp_tool(requires_auth=True)
    async def protected_op() -> None:
        pass

    assert protected_op.spec.requires_auth is True


def test_decorator_preserves_callable():
    @mcp_tool()
    async def echo(x: str) -> str:
        return x

    import asyncio

    assert asyncio.run(echo("hi")) == "hi"


def test_build_mcp_server_raises_import_error_when_mcp_missing(monkeypatch):
    """build_mcp_server raises ImportError with helpful message when mcp is not installed."""
    import sys
    import unittest.mock as mock

    from eap_core.mcp.registry import McpToolRegistry

    reg = McpToolRegistry()

    # Temporarily make the 'mcp' import fail
    with mock.patch.dict(sys.modules, {"mcp": None, "mcp.server": None, "mcp.types": None}):
        import pytest as _pytest

        from eap_core.mcp.server import build_mcp_server

        with _pytest.raises(ImportError, match="mcp"):
            build_mcp_server(reg)
