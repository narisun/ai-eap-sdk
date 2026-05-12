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


def test_transport_only_accepts_stdio_in_v1_1():
    """Forward-compat pin: when v1.2 adds 'http', this test should be
    updated to assert both literals are accepted. If a future commit
    silently broadens the Literal without bumping the minor version,
    this test catches it."""
    with pytest.raises(ValidationError, match="literal"):
        McpServerConfig.model_validate({"name": "x", "command": "python", "transport": "http"})


def test_dict_roundtrip():
    cfg = McpServerConfig(name="x", command="python", args=["a"], request_timeout_s=10.0)
    d = cfg.model_dump()
    back = McpServerConfig.model_validate(d)
    assert back == cfg
