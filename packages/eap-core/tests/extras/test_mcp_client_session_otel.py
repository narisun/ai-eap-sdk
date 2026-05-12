"""Extras-path tests for McpClientSession's OTel integration.

The non-extras suite (``test_mcp_client_session.py``) covers behaviour,
typed-error mapping, and timeout semantics with the OTel API absent —
i.e. the no-op span path. This file covers the *span-attribute*
contract: when OTel is importable, the session must populate
``mcp.server.name``, ``mcp.tool.name``, ``mcp.duration_s`` on success
and ``mcp.error.kind`` + ``mcp.error.class`` plus a Status(ERROR, kind)
on failure.

Following the FakeSpan / FakeTracer pattern from
``tests/test_observability.py``: we monkeypatch the session module's
``_otel_tracer`` factory to return a FakeTracer that produces FakeSpans
recording every attribute write. This way we assert on the spans the
production code would have emitted without needing a real OTel SDK
exporter — the SDK path is covered by symmetric tests elsewhere.

We still importorskip ``opentelemetry`` here so the ``Status`` /
``StatusCode`` import inside ``_record_span_error`` succeeds; without
the API installed that branch silently skips and we couldn't assert on
status_set.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("opentelemetry")
pytestmark = pytest.mark.extras

from eap_core.mcp.client import (
    McpServerDisconnectedError,
    McpToolTimeoutError,
)
from eap_core.mcp.client import session as session_mod
from eap_core.mcp.client.session import McpClientSession


class FakeSpan:
    """Mirrors the FakeSpan pattern in ``tests/test_observability.py``
    — records every attribute write, every status change, and the
    ``end()`` call so the test can assert exactly what production set."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attrs: dict[str, Any] = {}
        self.status_set: Any = None
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def set_status(self, status: Any) -> None:
        self.status_set = status

    def end(self) -> None:
        self.ended = True


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_span(self, name: str) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


class _StubUpstream:
    """Tiny duck-typed upstream — same shape as the non-extras suite's
    stub but inlined here so this file is self-contained for the
    optional-extras runner."""

    def __init__(
        self,
        *,
        call_tool_response: Any = None,
        call_tool_exc: Exception | None = None,
        call_tool_delay_s: float = 0.0,
    ) -> None:
        self._response = call_tool_response
        self._exc = call_tool_exc
        self._delay = call_tool_delay_s

    async def list_tools(self) -> Any:  # pragma: no cover - not exercised here
        return SimpleNamespace(tools=[])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        return self._response


@pytest.fixture
def fake_tracer(monkeypatch: pytest.MonkeyPatch) -> FakeTracer:
    tracer = FakeTracer()
    monkeypatch.setattr(session_mod, "_otel_tracer", lambda: tracer)
    return tracer


async def test_success_path_sets_server_tool_and_duration_attributes(
    fake_tracer: FakeTracer,
) -> None:
    upstream = _StubUpstream(
        call_tool_response=SimpleNamespace(content=[SimpleNamespace(text='{"ok": 1}')])
    )
    session = McpClientSession(server_name="bankdw", upstream=upstream, request_timeout_s=1.0)
    await session.call_tool("query_sql", {"sql": "SELECT 1"})

    assert len(fake_tracer.spans) == 1
    span = fake_tracer.spans[0]
    assert span.name == "mcp.client.call_tool"
    assert span.attrs["mcp.server.name"] == "bankdw"
    assert span.attrs["mcp.tool.name"] == "query_sql"
    assert "mcp.duration_s" in span.attrs
    assert isinstance(span.attrs["mcp.duration_s"], float)
    assert span.attrs["mcp.duration_s"] >= 0
    # Success path doesn't set status / error attributes.
    assert span.status_set is None
    assert "mcp.error.kind" not in span.attrs
    assert "mcp.error.class" not in span.attrs
    assert span.ended


async def test_timeout_path_sets_error_kind_timeout_and_error_status(
    fake_tracer: FakeTracer,
) -> None:
    upstream = _StubUpstream(call_tool_delay_s=0.5)
    session = McpClientSession(server_name="bankdw", upstream=upstream, request_timeout_s=0.05)
    with pytest.raises(McpToolTimeoutError):
        await session.call_tool("slow", {})

    assert len(fake_tracer.spans) == 1
    span = fake_tracer.spans[0]
    assert span.attrs["mcp.error.kind"] == "timeout"
    # The original asyncio.TimeoutError is what the span records — the
    # typed McpToolTimeoutError is what the caller sees. The two layers
    # are separate by design.
    assert span.attrs["mcp.error.class"] == "TimeoutError"
    # Status was set to ERROR with the kind as description.
    from opentelemetry.trace import StatusCode

    assert span.status_set is not None
    assert span.status_set.status_code == StatusCode.ERROR
    # Even on the error path the span is ended in the outer ``finally``
    # so OTel exporters don't see a leaked span.
    assert span.ended
    assert "mcp.duration_s" in span.attrs


async def test_disconnect_path_sets_error_kind_disconnected(
    fake_tracer: FakeTracer,
) -> None:
    upstream = _StubUpstream(call_tool_exc=BrokenPipeError())
    session = McpClientSession(server_name="sfcrm", upstream=upstream, request_timeout_s=1.0)
    with pytest.raises(McpServerDisconnectedError):
        await session.call_tool("list_tables", {})

    assert len(fake_tracer.spans) == 1
    span = fake_tracer.spans[0]
    assert span.attrs["mcp.error.kind"] == "disconnected"
    assert span.attrs["mcp.error.class"] == "BrokenPipeError"
    assert span.attrs["mcp.server.name"] == "sfcrm"
    assert span.attrs["mcp.tool.name"] == "list_tables"
    from opentelemetry.trace import StatusCode

    assert span.status_set is not None
    assert span.status_set.status_code == StatusCode.ERROR
    assert span.ended


async def test_invocation_error_path_sets_error_kind_invocation_error(
    fake_tracer: FakeTracer,
) -> None:
    """Symmetry check for the catch-all branch — any non-disconnect,
    non-timeout upstream failure should still produce a typed span with
    ``mcp.error.kind="invocation_error"`` so a tracing UI can
    distinguish "unknown failure" from the explicit cases."""
    upstream = _StubUpstream(call_tool_exc=RuntimeError("server kaboom"))
    session = McpClientSession(server_name="bankdw", upstream=upstream, request_timeout_s=1.0)
    from eap_core.mcp.client import McpToolInvocationError

    with pytest.raises(McpToolInvocationError):
        await session.call_tool("query_sql", {})

    span = fake_tracer.spans[0]
    assert span.attrs["mcp.error.kind"] == "invocation_error"
    assert span.attrs["mcp.error.class"] == "RuntimeError"
    assert span.ended
