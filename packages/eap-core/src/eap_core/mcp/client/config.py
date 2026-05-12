"""Configuration types for the MCP client subpackage.

``McpServerConfig`` is a discriminated union over ``transport``. v1.0
shipped only the ``stdio`` variant; v1.2 adds ``http`` for the
Streamable-HTTP transport; v1.3 adds ``sse`` (legacy SSE) and
``websocket``. A pydantic model_validator enforces that each variant
has the required transport-specific fields and rejects the wrong
ones.

The ``http`` and ``sse`` variants share the same shape (``url`` +
optional ``headers``/``auth``) — the model_validator treats them as a
single group. The ``websocket`` variant is URL-only because the
upstream ``mcp.client.websocket.websocket_client`` takes only a URL
parameter (no headers or auth); this limitation is enforced via
explicit rejection so callers see a loud failure rather than silent
header drop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class McpServerConfig(BaseModel):
    """One MCP server's connection parameters.

    Four transports supported as of v1.3:

    - ``transport="stdio"`` — spawns a subprocess; the SDK runs the
      server's binary and talks over the subprocess's stdin/stdout.
      Required: ``command``. Optional: ``args``, ``cwd``, ``env``.
    - ``transport="http"`` — opens a Streamable-HTTP session to a
      remote MCP server (the modern HTTP protocol). Required:
      ``url``. Optional: ``headers``, ``auth``.
    - ``transport="sse"`` — opens a legacy SSE session to a remote
      MCP server (the predecessor to Streamable-HTTP; still in use by
      some servers). Required: ``url``. Optional: ``headers``,
      ``auth``.
    - ``transport="websocket"`` — opens an MCP-over-WebSocket
      session. Required: ``url``. The upstream ``websocket_client``
      currently takes URL only — no ``headers`` or ``auth`` — so
      authenticated WebSocket MCP servers must encode credentials in
      the URL itself.

    Examples::

        # stdio (canonical local-dev shape)
        McpServerConfig(
            name="bankdw",
            command="python",
            args=["server.py"],
            cwd=Path("examples/bankdw-mcp-server"),
        )

        # http (production-deployed remote MCP server)
        McpServerConfig(
            name="bankdw",
            transport="http",
            url="https://bankdw.example.com/mcp",
            headers={"X-API-Key": "..."},
        )

        # sse (legacy SSE remote server)
        McpServerConfig(
            name="legacy",
            transport="sse",
            url="https://legacy.example.com/sse",
        )
    """

    name: str = Field(
        min_length=1,
        description=(
            "Logical name used to namespace this server's tools in the "
            "local registry. Tool names become ``<server-name>__<tool-name>``."
        ),
    )
    transport: Literal["stdio", "http", "sse", "websocket"] = Field(
        default="stdio",
        description=(
            "Transport mechanism. ``stdio`` spawns a subprocess; ``http`` "
            "opens a Streamable-HTTP session (the modern HTTP protocol); "
            "``sse`` opens a legacy SSE session; ``websocket`` opens an "
            "MCP-over-WebSocket session."
        ),
    )

    # stdio-only fields
    command: str | None = Field(
        default=None,
        description="Executable path or program name for stdio transport.",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Arguments passed to ``command`` (stdio only).",
    )
    cwd: Path | None = Field(
        default=None,
        description="Working directory for the stdio subprocess. None = inherit.",
    )
    env: dict[str, str] | None = Field(
        default=None,
        description="Environment overrides for the stdio subprocess. None = inherit parent.",
    )

    # http-only fields
    url: str | None = Field(
        default=None,
        description="MCP server URL for http transport (e.g. https://mcp.example.com/v1).",
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description=(
            "HTTP headers attached to every request. Use this for "
            "static auth tokens, API keys, or any vendor-specific "
            "header the remote server requires."
        ),
    )
    auth: Any = Field(
        default=None,
        description=(
            "Optional ``httpx.Auth`` instance for richer authentication "
            "schemes than static headers (Bearer rotation, OAuth flows, "
            "etc.). Pass through to the upstream Streamable-HTTP client. "
            "Runtime expectation: an ``httpx.Auth`` instance or ``None``. "
            "Typed as ``Any`` to avoid forcing ``httpx`` into the core "
            "import path; excluded from ``model_dump`` because "
            "``httpx.Auth`` instances are not JSON-serialisable."
        ),
        exclude=True,  # not JSON-serialisable; excluded from model_dump
    )

    # transport-agnostic fields
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

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> McpServerConfig:
        """Enforce transport-specific field requirements:

        - ``stdio``: ``command`` required; ``url``/``headers``/``auth``
          forbidden (would silently be ignored otherwise).
        - ``http``/``sse``: ``url`` required; ``command``/``args``/
          ``cwd``/``env`` forbidden (no subprocess to configure). Both
          transports share the same shape — remote MCP server reached
          via httpx with optional headers and auth.
        - ``websocket``: ``url`` required; ``command``/``args``/``cwd``/
          ``env``/``headers``/``auth`` all forbidden. The upstream
          ``websocket_client`` takes URL only, so any auth-y field
          would silently drop; we reject loudly to surface the
          limitation.
        """
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("transport='stdio' requires command (the executable to run)")
            for field_name in ("url", "headers", "auth"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"transport='stdio' forbids {field_name!r} (http/sse-only field)"
                    )
        elif self.transport in ("http", "sse"):
            if not self.url:
                raise ValueError(
                    f"transport={self.transport!r} requires url (the MCP server endpoint)"
                )
            for field_name in ("command", "cwd", "env"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"transport={self.transport!r} forbids {field_name!r} (stdio-only field)"
                    )
            if self.args:
                raise ValueError(f"transport={self.transport!r} forbids 'args' (stdio-only field)")
        elif self.transport == "websocket":
            if not self.url:
                raise ValueError("transport='websocket' requires url (the MCP server endpoint)")
            for field_name in ("command", "cwd", "env", "headers", "auth"):
                if getattr(self, field_name) is not None:
                    raise ValueError(
                        f"transport='websocket' forbids {field_name!r} — the upstream "
                        f"``websocket_client`` takes only url; auth via URL query params "
                        f"if needed (no clean Authorization header convention in WebSockets)"
                    )
            if self.args:
                raise ValueError("transport='websocket' forbids 'args' (stdio-only field)")
        return self
