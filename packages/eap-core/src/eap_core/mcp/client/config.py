"""Configuration types for the MCP client subpackage.

``McpServerConfig`` replaces the v1.0 example shim's ``dict[str, Any]``
config. It validates at construction time, supports a ``transport``
discriminator so future v1.x can add HTTP/SSE without breaking the
public API, and serialises to/from dict for use in config files.

The current ``transport`` discriminator accepts only ``"stdio"``. v1.2
can extend the Literal to include ``"http"`` etc.; existing callers
using the default value or ``transport="stdio"`` keep working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    """One MCP server's connection parameters.

    Examples:
        cfg = McpServerConfig(
            name="bankdw",
            command="python",
            args=["server.py"],
            cwd=Path("examples/bankdw-mcp-server"),
        )
    """

    name: str = Field(
        min_length=1,
        description=(
            "Logical name used to namespace this server's tools in the "
            "local registry. Tool names become ``<server-name>__<tool-name>``."
        ),
    )
    transport: Literal["stdio"] = Field(
        default="stdio",
        description="Transport mechanism. Only ``stdio`` supported in v1.1.",
    )
    command: str = Field(
        description="Executable path or program name (e.g. ``python``, ``node``).",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Arguments passed to ``command``.",
    )
    cwd: Path | None = Field(
        default=None,
        description="Working directory for the subprocess. None = inherit.",
    )
    env: dict[str, str] | None = Field(
        default=None,
        description="Environment overrides. None = inherit parent.",
    )
    request_timeout_s: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Per-call timeout. Applied around every ``call_tool``; "
            "exceeding it raises ``McpToolTimeoutError``."
        ),
    )
    validate_output_schemas: bool = Field(
        default=False,
        description=(
            "If True, validate every remote tool's response against the "
            "``outputSchema`` advertised in ``tools/list``. Failures raise "
            "``McpOutputSchemaError``. Default False because most remote "
            "tools don't publish outputSchema; opt in when you trust the "
            "remote server to keep its schema honest."
        ),
    )
