"""Wraps an upstream ``mcp.ClientSession`` with timeout + observability
+ typed error mapping.

This module is the only place where ``mcp.client`` is imported lazily.
``McpClientSession`` is a thin asyncio wrapper: the upstream session
already handles framing, request/response correlation, etc. We add:

- Per-call timeout (configurable per server via ``request_timeout_s``).
- An OpenTelemetry span around every ``call_tool``, with attributes
  for the server name, tool name, duration, and error class on failure.
  Span emission is best-effort — if OTel isn't installed the spans are
  no-ops.
- Translation of upstream errors into the typed ``McpClientError``
  hierarchy. Callers should not need to catch ``mcp``-specific
  exceptions.

The observability helpers mirror ``eap_core.middleware.observability``
(the server-side OTel integration): same lazy-import style, same
attribute-key prefixing (``mcp.*`` here, ``gen_ai.*`` there), same
``Status(StatusCode.ERROR, ...)`` pattern on failure. The goal is that
a downstream observer can follow a single trace from an EAP-Core agent
through this client session, across stdio, into the remote server's
own observability middleware — and see a coherent span tree with
consistent attribute conventions.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from eap_core.mcp.client.errors import (
    McpServerDisconnectedError,
    McpToolInvocationError,
    McpToolTimeoutError,
)


class McpClientSession:
    """Per-server session handle. One instance per running subprocess.

    Not constructed directly by user code — see :class:`McpClientPool`.
    The class is importable from ``eap_core.mcp.client.session`` for
    advanced callers and tests, but it isn't in the public ``__all__``.

    The ``upstream`` parameter is duck-typed as :class:`typing.Any`
    rather than ``mcp.ClientSession`` so tests can pass stub objects
    without importing ``mcp`` on the non-extras path. The runtime
    contract is "an object with ``async list_tools()`` and ``async
    call_tool(name, arguments)`` methods that match ``mcp.ClientSession``'s
    signatures."
    """

    def __init__(
        self,
        *,
        server_name: str,
        upstream: Any,
        request_timeout_s: float,
    ) -> None:
        self._name = server_name
        self._upstream = upstream
        self._timeout_s = request_timeout_s

    @property
    def name(self) -> str:
        return self._name

    async def list_tools(self) -> list[Any]:
        """Return the remote server's tool list (raw ``mcp.types.Tool``
        objects). Used by the adapter to build local ``ToolSpec``s.
        """
        try:
            response = await self._upstream.list_tools()
        except (ConnectionError, BrokenPipeError, EOFError) as e:
            raise McpServerDisconnectedError(
                f"server {self._name!r} disconnected during list_tools"
            ) from e
        return list(response.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a remote tool with timeout + observability + typed errors.

        Returns the upstream ``CallToolResult``. The caller (adapter
        layer) is responsible for decoding ``response.content``.

        Error mapping precedence (specific -> general):

        1. ``asyncio.TimeoutError`` -> ``McpToolTimeoutError``.
        2. ``ConnectionError`` / ``BrokenPipeError`` / ``EOFError`` ->
           ``McpServerDisconnectedError`` (signal to reconnect).
        3. Catch-all ``Exception`` -> ``McpToolInvocationError``.

        The catch-all goes last so it doesn't shadow the typed branches
        above — each typed handler raises before the catch-all sees the
        exception.
        """
        span = _start_span(self._name, name)
        start = time.perf_counter()
        try:
            try:
                response = await asyncio.wait_for(
                    self._upstream.call_tool(name, arguments),
                    timeout=self._timeout_s,
                )
            except TimeoutError as e:
                # ``asyncio.wait_for`` raises ``asyncio.TimeoutError``,
                # which is an alias for the builtin ``TimeoutError`` on
                # Python 3.11+. Catching the builtin keeps ruff's UP041
                # happy and works for either name.
                _record_span_error(span, e, "timeout")
                raise McpToolTimeoutError(tool=name, timeout_s=self._timeout_s) from e
            except (ConnectionError, BrokenPipeError, EOFError) as e:
                _record_span_error(span, e, "disconnected")
                raise McpServerDisconnectedError(
                    f"server {self._name!r} disconnected during call_tool({name!r})"
                ) from e
            except Exception as e:
                # Catch-all so any upstream-specific exception becomes
                # an McpToolInvocationError. We deliberately don't broaden
                # the typed errors above into this branch — the specific
                # cases need targeted handling at higher layers (pool
                # reconnect, caller-level retry).
                _record_span_error(span, e, "invocation_error")
                raise McpToolInvocationError(
                    f"server {self._name!r} tool {name!r} raised: {e}"
                ) from e
        finally:
            duration_s = time.perf_counter() - start
            _end_span(span, duration_s)
        return response


# ---------------------------------------------------------------------------
# Observability — best-effort spans. When the [otel] extra isn't installed,
# every function below is a no-op. Mirrors the lazy-import + None-fallback
# pattern used by ``eap_core.middleware.observability`` on the server side.
# ---------------------------------------------------------------------------


def _otel_tracer() -> Any | None:
    """Return an OTel tracer or None if OTel API isn't importable.

    Note: we import the API (no SDK required). If a downstream agent
    has the OTel SDK configured, our spans flow through it; if not,
    the API's no-op tracer would still be returned — but we keep the
    None-on-ImportError contract so the module is fully zero-cost when
    the extra isn't installed.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("eap_core.mcp.client")


def _start_span(server_name: str, tool_name: str) -> Any | None:
    tracer = _otel_tracer()
    if tracer is None:
        return None
    span = tracer.start_span("mcp.client.call_tool")
    span.set_attribute("mcp.server.name", server_name)
    span.set_attribute("mcp.tool.name", tool_name)
    return span


def _end_span(span: Any | None, duration_s: float) -> None:
    if span is None:
        return
    span.set_attribute("mcp.duration_s", duration_s)
    span.end()


def _record_span_error(span: Any | None, exc: Exception, kind: str) -> None:
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        span.set_status(Status(StatusCode.ERROR, kind))
    except ImportError:
        pass
    span.set_attribute("mcp.error.kind", kind)
    span.set_attribute("mcp.error.class", type(exc).__name__)
