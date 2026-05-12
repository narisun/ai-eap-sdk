"""Regression test for unary lifecycle hook on_call_end (Finding 2)."""

from __future__ import annotations

import pytest

from eap_core.middleware.base import Middleware, PassthroughMiddleware
from eap_core.middleware.pipeline import MiddlewarePipeline
from eap_core.types import Context, Request, Response


class _TraceMW(PassthroughMiddleware):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    async def on_request(self, req: Request, ctx: Context) -> Request:
        self._log.append(f"{self.name}:on_request")
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        self._log.append(f"{self.name}:on_response")
        return resp

    async def on_call_end(self, ctx: Context) -> None:
        self._log.append(f"{self.name}:on_call_end")

    async def on_error(self, exc: Exception, ctx: Context) -> None:
        self._log.append(f"{self.name}:on_error")


async def _ok_terminal(_req: Request, _ctx: Context) -> Response:
    return Response(text="ok")


async def _boom_terminal(_req: Request, _ctx: Context) -> Response:
    raise RuntimeError("boom")


async def test_run_fires_on_call_end_after_response() -> None:
    """Happy path: on_call_end fires right-to-left AFTER on_response."""
    log: list[str] = []
    mws: list[Middleware] = [_TraceMW("A", log), _TraceMW("B", log)]
    pipeline = MiddlewarePipeline(mws)
    req = Request(model="m", messages=[])

    await pipeline.run(req, Context(request_id="r"), _ok_terminal)

    assert log[:2] == ["A:on_request", "B:on_request"]
    # on_response fires R→L immediately after terminal.
    # on_call_end fires R→L in finally, AFTER on_response.
    assert log[2:] == ["B:on_response", "A:on_response", "B:on_call_end", "A:on_call_end"]


async def test_run_fires_on_call_end_even_on_terminal_exception() -> None:
    """on_call_end MUST fire when terminal raises, BEFORE on_error."""
    log: list[str] = []
    mws: list[Middleware] = [_TraceMW("A", log)]
    pipeline = MiddlewarePipeline(mws)
    req = Request(model="m", messages=[])

    with pytest.raises(RuntimeError, match="boom"):
        await pipeline.run(req, Context(request_id="r"), _boom_terminal)

    assert log == ["A:on_request", "A:on_call_end", "A:on_error"]
    # No on_response on the failure path (correct existing behavior).
    assert "A:on_response" not in log


async def test_run_on_call_end_secondary_does_not_mask_primary() -> None:
    """If on_call_end itself raises, the primary terminal exception still propagates."""
    log: list[str] = []

    class _BadCleanup(PassthroughMiddleware):
        name = "bad"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            log.append("bad:on_request")
            return req

        async def on_call_end(self, ctx: Context) -> None:
            log.append("bad:on_call_end_raises")
            raise RuntimeError("cleanup blew up")

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            log.append("bad:on_error")

    pipeline = MiddlewarePipeline([_BadCleanup()])
    req = Request(model="m", messages=[])

    # Primary must be the terminal error, not the cleanup error.
    with pytest.raises(RuntimeError, match="boom"):
        await pipeline.run(req, Context(request_id="r"), _boom_terminal)

    assert "bad:on_call_end_raises" in log
    assert "bad:on_error" in log  # on_error still fires for terminal exc
