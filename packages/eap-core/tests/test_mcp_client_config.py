"""Tests for McpServerConfig validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eap_core.mcp.client import McpServerConfig


def test_minimal_config_construction():
    cfg = McpServerConfig(name="x", command="python")
    assert cfg.name == "x"
    assert cfg.transport == "stdio"
    assert cfg.args == []
    assert cfg.cwd is None
    assert cfg.env is None
    assert cfg.request_timeout_s == 30.0
    assert cfg.validate_output_schemas is False


def test_full_config_construction():
    cfg = McpServerConfig(
        name="bankdw",
        command="python",
        args=["server.py", "--verbose"],
        cwd=Path("/tmp/bankdw"),
        env={"FOO": "bar"},
        request_timeout_s=5.0,
        validate_output_schemas=True,
    )
    assert cfg.args == ["server.py", "--verbose"]
    assert cfg.cwd == Path("/tmp/bankdw")
    assert cfg.env == {"FOO": "bar"}
    assert cfg.request_timeout_s == 5.0
    assert cfg.validate_output_schemas is True


def test_name_must_be_non_empty():
    with pytest.raises(ValidationError, match="at least 1 character"):
        McpServerConfig(name="", command="python")


def test_request_timeout_must_be_positive():
    with pytest.raises(ValidationError, match="greater than 0"):
        McpServerConfig(name="x", command="python", request_timeout_s=0)


def test_request_timeout_zero_is_rejected():
    with pytest.raises(ValidationError):
        McpServerConfig(name="x", command="python", request_timeout_s=-1.0)


def test_transport_only_accepts_stdio_or_http_in_v1_2():
    """Forward-compat pin: v1.2 accepts 'stdio' and 'http'. When a future
    minor adds 'websocket' (or similar), this test should be updated to
    assert the new value is accepted. If a future commit silently
    broadens the Literal without bumping the minor version, this test
    catches it."""
    with pytest.raises(ValidationError, match="literal"):
        McpServerConfig.model_validate({"name": "x", "command": "python", "transport": "websocket"})


def test_dict_roundtrip():
    cfg = McpServerConfig(name="x", command="python", args=["a"], request_timeout_s=10.0)
    d = cfg.model_dump()
    back = McpServerConfig.model_validate(d)
    assert back == cfg


# v1.2: http transport variant tests


def test_http_config_minimal():
    cfg = McpServerConfig(
        name="remote",
        transport="http",
        url="https://mcp.example.com",
    )
    assert cfg.transport == "http"
    assert cfg.url == "https://mcp.example.com"
    assert cfg.command is None
    assert cfg.headers is None
    assert cfg.auth is None


def test_http_config_with_headers():
    cfg = McpServerConfig(
        name="r",
        transport="http",
        url="https://x",
        headers={"X-API-Key": "secret"},
    )
    assert cfg.headers == {"X-API-Key": "secret"}


def test_stdio_config_rejects_url():
    with pytest.raises(ValidationError, match="forbids 'url'"):
        McpServerConfig(name="x", command="python", url="https://x")


def test_stdio_config_rejects_headers():
    with pytest.raises(ValidationError, match="forbids 'headers'"):
        McpServerConfig(name="x", command="python", headers={"X-A": "B"})


def test_stdio_config_rejects_auth():
    sentinel = object()
    with pytest.raises(ValidationError, match="forbids 'auth'"):
        McpServerConfig(name="x", command="python", auth=sentinel)


def test_http_config_rejects_command():
    with pytest.raises(ValidationError, match="forbids 'command'"):
        McpServerConfig(name="x", transport="http", url="https://x", command="python")


def test_http_config_rejects_args():
    with pytest.raises(ValidationError, match="forbids 'args'"):
        McpServerConfig(name="x", transport="http", url="https://x", args=["a"])


def test_http_config_rejects_cwd():
    with pytest.raises(ValidationError, match="forbids 'cwd'"):
        McpServerConfig(name="x", transport="http", url="https://x", cwd=Path("/tmp"))


def test_http_config_rejects_env():
    with pytest.raises(ValidationError, match="forbids 'env'"):
        McpServerConfig(name="x", transport="http", url="https://x", env={"FOO": "bar"})


def test_http_config_requires_url():
    with pytest.raises(ValidationError, match="requires url"):
        McpServerConfig(name="x", transport="http")


def test_stdio_config_requires_command():
    with pytest.raises(ValidationError, match="requires command"):
        McpServerConfig(name="x", transport="stdio")


def test_http_dict_roundtrip_excludes_auth():
    """L2 pin: ``auth`` is declared with ``exclude=True`` because
    ``httpx.Auth`` instances aren't JSON-serialisable. A round-trip
    through ``model_dump() -> model_validate()`` must succeed and the
    reconstructed config must have ``auth=None`` (the original auth
    value is dropped intentionally; callers re-attach it after
    deserialisation if needed)."""

    class FakeHttpxAuth:
        """Stand-in for httpx.Auth so we don't import httpx here."""

    cfg = McpServerConfig(
        name="remote",
        transport="http",
        url="https://mcp.example.com",
        headers={"X-API-Key": "secret"},
        auth=FakeHttpxAuth(),
    )
    d = cfg.model_dump()
    assert "auth" not in d
    back = McpServerConfig.model_validate(d)
    assert back.transport == "http"
    assert back.url == "https://mcp.example.com"
    assert back.headers == {"X-API-Key": "secret"}
    assert back.auth is None
