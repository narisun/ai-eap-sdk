"""Onion-model executor for the middleware chain."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from eap_core.types import Chunk, Context, Request, Response

if TYPE_CHECKING:
    from eap_core.middleware.base import Middleware

Terminal = Callable[[Request, Context], Awaitable[Response]]
StreamTerminal = Callable[[Request, Context], AsyncIterator[Chunk]]


class MiddlewarePipeline:
    """Chain-of-responsibility executor.

    Runs `on_request` left-to-right, invokes the terminal callable, then
    `on_response` right-to-left. On exception, runs `on_error` in reverse
    order on every middleware whose `on_request` already executed.
    """

    def __init__(self, middlewares: list[Middleware]) -> None:
        self._mws = list(middlewares)

    async def run(self, req: Request, ctx: Context, terminal: Terminal) -> Response:
        ran: list[Middleware] = []
        try:
            for mw in self._mws:
                ran.append(mw)
                req = await mw.on_request(req, ctx)
            resp = await terminal(req, ctx)
            for mw in reversed(ran):
                resp = await mw.on_response(resp, ctx)
            return resp
        except Exception as exc:
            for mw in reversed(ran):
                try:
                    await mw.on_error(exc, ctx)
                except Exception:  # noqa: S110
                    pass
            raise

    async def run_stream(
        self, req: Request, ctx: Context, terminal: StreamTerminal
    ) -> AsyncIterator[Chunk]:
        ran: list[Middleware] = []
        try:
            for mw in self._mws:
                req = await mw.on_request(req, ctx)
                ran.append(mw)
            async for chunk in terminal(req, ctx):
                for mw in self._mws:
                    chunk = await mw.on_stream_chunk(chunk, ctx)
                yield chunk
        except Exception as exc:
            for mw in reversed(ran):
                try:
                    await mw.on_error(exc, ctx)
                except Exception:  # noqa: S110
                    pass
            raise
