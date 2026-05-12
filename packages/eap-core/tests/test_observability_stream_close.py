"""Regression tests for ObservabilityMiddleware streaming span close.

v1.6.2 CHANGELOG flagged: on streaming success path, ctx.span leaks
because on_response (unary-only) never fires and on_error (failure-only)
doesn't fire either. T3 wires the close into on_stream_end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.types import Chunk, Context, Request


def _mock_span() -> MagicMock:
    """Build a span mock recording end()/set_attribute() invocations."""
    span = MagicMock()
    span.set_attribute = MagicMock()
    span.end = MagicMock()
    return span


async def test_observability_closes_span_on_stream_success() -> None:
    """ctx.span must be None after run_stream completes normally."""
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    ctx.span = _mock_span()
    pipeline = MiddlewarePipeline([mw])

    async def _terminal(_req: Request, _ctx: Context) -> AsyncIterator[Chunk]:
        yield Chunk(index=0, text="hi", finish_reason="stop")

    req = Request(model="m", messages=[], stream=True)
    chunks = []
    async for c in pipeline.run_stream(req, ctx, _terminal):
        chunks.append(c)

    assert ctx.span is None, "span must be closed at on_stream_end on success path"


async def test_observability_closes_span_on_stream_exception() -> None:
    """ctx.span must be None even when terminal raises mid-iteration."""
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    ctx.span = _mock_span()
    pipeline = MiddlewarePipeline([mw])

    async def _explodes(_req: Request, _ctx: Context) -> AsyncIterator[Chunk]:
        yield Chunk(index=0, text="hi", finish_reason=None)
        raise RuntimeError("upstream boom")

    req = Request(model="m", messages=[], stream=True)

    with pytest.raises(RuntimeError, match="upstream boom"):
        async for _ in pipeline.run_stream(req, ctx, _explodes):
            pass

    assert ctx.span is None, "span must be closed even after exception"


async def test_observability_stream_end_is_idempotent_with_on_error() -> None:
    """If on_error closes the span first, on_stream_end must not raise or double-close."""
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    span = _mock_span()
    ctx.span = span

    # Simulate the exception flow: on_error runs first (closes span),
    # then on_stream_end fires from the finally block.
    await mw.on_error(RuntimeError("boom"), ctx)
    assert ctx.span is None, "on_error should have closed the span"

    # Now on_stream_end runs — must noop cleanly.
    await mw.on_stream_end(ctx)  # MUST NOT raise

    # span.end() should have been called exactly once (by on_error).
    assert span.end.call_count == 1, (
        "on_stream_end double-closed the span; guard ctx.span is None must be present"
    )


async def test_observability_stream_end_noops_when_span_never_set() -> None:
    """If a request runs without observability's on_request (e.g., span
    construction failed silently), on_stream_end must still be safe."""
    mw = ObservabilityMiddleware()
    ctx = Context(request_id="r")
    assert ctx.span is None  # never set
    await mw.on_stream_end(ctx)  # no exception, no behavior
    assert ctx.span is None
