"""Regression test for streaming end-of-stream lifecycle (P0-3)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from eap_core.middleware.base import Middleware, PassthroughMiddleware
from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.types import Chunk, Context, Request, Response


class _TraceMW(PassthroughMiddleware):
    """Records every lifecycle hook firing."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    async def on_request(self, req: Request, ctx: Context) -> Request:
        self._log.append(f"{self.name}:on_request")
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        self._log.append(f"{self.name}:on_response")
        return resp

    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
        self._log.append(f"{self.name}:on_stream_chunk")
        return chunk

    async def on_stream_end(self, ctx: Context) -> None:
        self._log.append(f"{self.name}:on_stream_end")

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        self._log.append(f"{self.name}:on_error")


async def _three_chunks(_req: Request, _ctx: Context) -> AsyncIterator[Chunk]:
    yield Chunk(index=0, text="a", finish_reason=None)
    yield Chunk(index=1, text="b", finish_reason=None)
    yield Chunk(index=2, text="c", finish_reason="stop")


async def test_run_stream_fires_on_stream_end_after_chunks() -> None:
    log: list[str] = []
    mws: list[Middleware] = [_TraceMW("A", log), _TraceMW("B", log)]
    pipeline = MiddlewarePipeline(mws)
    req = Request(model="m", messages=[], stream=True)

    chunks = []
    async for c in pipeline.run_stream(req, Context(request_id="r"), _three_chunks):
        chunks.append(c)

    assert len(chunks) == 3
    # on_request runs left-to-right, on_stream_end runs right-to-left,
    # mirroring on_response semantics from run().
    assert log[:2] == ["A:on_request", "B:on_request"]
    assert log[-2:] == ["B:on_stream_end", "A:on_stream_end"]
    # No stray on_response on the streaming path.
    assert not any("on_response" in entry for entry in log)
    # Exactly one on_stream_end per middleware.
    assert log.count("A:on_stream_end") == 1
    assert log.count("B:on_stream_end") == 1


async def test_run_stream_fires_on_stream_end_even_on_terminal_exception() -> None:
    """If the terminal stream raises mid-iteration, on_stream_end MUST still fire."""
    log: list[str] = []

    async def _explodes(_req: Request, _ctx: Context) -> AsyncIterator[Chunk]:
        yield Chunk(index=0, text="a", finish_reason=None)
        raise RuntimeError("upstream blew up")

    mw = _TraceMW("A", log)
    pipeline = MiddlewarePipeline([mw])
    req = Request(model="m", messages=[], stream=True)

    with pytest.raises(RuntimeError, match="upstream blew up"):
        async for _ in pipeline.run_stream(req, Context(request_id="r"), _explodes):
            pass

    # on_stream_end MUST fire (audit close, span close, PII flush all
    # depend on it); on_error fires too because the terminal raised.
    assert "A:on_stream_end" in log
    assert "A:on_error" in log
