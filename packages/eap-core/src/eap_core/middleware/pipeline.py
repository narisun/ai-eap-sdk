"""Onion-model executor for the middleware chain."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from eap_core.types import Chunk, Context, Request, Response

if TYPE_CHECKING:
    from eap_core.middleware.base import Middleware

Terminal = Callable[[Request, Context], Awaitable[Response]]
StreamTerminal = Callable[[Request, Context], AsyncIterator[Chunk]]

_LOG = logging.getLogger(__name__)


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
            try:
                for mw in self._mws:
                    ran.append(mw)
                    req = await mw.on_request(req, ctx)
                resp = await terminal(req, ctx)
                for mw in reversed(ran):
                    resp = await mw.on_response(resp, ctx)
                return resp
            finally:
                # ``on_call_end`` runs right-to-left, best-effort, in a
                # ``finally`` block so it fires on BOTH normal completion AND
                # failure — mirrors ``on_stream_end`` semantics on the
                # streaming path. A secondary failure here must not mask the
                # primary terminal exception (dev-guide §3.2).
                for mw in reversed(ran):
                    try:
                        await mw.on_call_end(ctx)
                    except Exception as secondary:
                        mw_name = getattr(mw, "name", type(mw).__name__)
                        _LOG.warning(
                            "secondary failure in %s.on_call_end during call finalization",
                            mw_name,
                            exc_info=secondary,
                        )
        except Exception as exc:
            await self._on_error(ran, exc, ctx)
            raise

    async def run_stream(
        self, req: Request, ctx: Context, terminal: StreamTerminal
    ) -> AsyncIterator[Chunk]:
        ran: list[Middleware] = []
        try:
            for mw in self._mws:
                # Append BEFORE awaiting so a middleware that raises in
                # ``on_request`` still receives ``on_error`` — mirrors the
                # symmetry of ``run`` above (dev-guide §3.2). Without this,
                # streaming requests silently skipped the on_error hook for
                # the very middleware that failed.
                ran.append(mw)
                req = await mw.on_request(req, ctx)
            try:
                async for chunk in terminal(req, ctx):
                    for mw in self._mws:
                        chunk = await mw.on_stream_chunk(chunk, ctx)
                    yield chunk
            finally:
                # ``on_stream_end`` runs right-to-left to mirror ``on_response``
                # semantics. It fires whether the stream completes normally OR
                # the terminal/chunk pipeline raises — audit close, span close,
                # PII vault flush, trajectory write all need a final hook.
                # Errors from ``on_stream_end`` are best-effort: a secondary
                # failure here should not mask a primary terminal exception.
                for mw in reversed(ran):
                    try:
                        await mw.on_stream_end(ctx)
                    except Exception as secondary:
                        mw_name = getattr(mw, "name", type(mw).__name__)
                        _LOG.warning(
                            "secondary failure in %s.on_stream_end during streaming finalization",
                            mw_name,
                            exc_info=secondary,
                        )
        except Exception as exc:
            await self._on_error(ran, exc, ctx)
            raise

    async def _on_error(self, ran: list[Middleware], exc: Exception, ctx: Context) -> None:
        """Run on_error in reverse order, surfacing any secondary failures.

        A middleware's own ``on_error`` may itself raise — historically we
        swallowed those with ``except Exception: pass``, which hid genuine
        bugs (timeouts, broken transports) from operators.

        We log every secondary at WARNING with ``exc_info``. We do NOT set
        ``exc.__context__ = secondary``: that is semantically inverted
        (``Y.__context__ = X`` reads as "Y was raised while handling X",
        but here ``secondary`` is the one raised while handling ``exc``),
        and assigning in a loop would clobber every prior secondary,
        leaving only the last one visible.

        Instead, we attach a PEP 678 note on the primary for each
        secondary. Notes survive re-raise and render at the bottom of the
        traceback, so operators see every middleware whose ``on_error``
        failed — without inverting Python's exception-chaining semantics.
        """
        for mw in reversed(ran):
            try:
                await mw.on_error(exc, ctx)
            except Exception as secondary:
                mw_name = getattr(mw, "name", type(mw).__name__)
                _LOG.warning(
                    "middleware %s.on_error raised: %s",
                    mw_name,
                    secondary,
                    exc_info=True,
                )
                # PEP 678: attach a note so the secondary is visible in
                # the traceback even though only the primary is re-raised.
                if hasattr(exc, "add_note"):
                    exc.add_note(
                        f"on_error secondary in {mw_name}: {type(secondary).__name__}: {secondary}"
                    )
